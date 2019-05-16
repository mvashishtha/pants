# coding=utf-8
# Copyright 2015 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

from __future__ import absolute_import, division, print_function, unicode_literals

import logging
import socket
import threading
import traceback

from six.moves.socketserver import BaseRequestHandler, BaseServer, TCPServer, ThreadingMixIn

from pants.engine.native import Native
from pants.java.nailgun_protocol import NailgunProtocol
from pants.util.contextutil import maybe_profiled
from pants.util.socket import RecvBufferedSocket, safe_select


class PailgunHandlerBase(BaseRequestHandler):
  """Base class for nailgun protocol handlers for use with SocketServer-based servers."""

  def __init__(self, request, client_address, server):
    """Override of BaseRequestHandler.__init__() that defers calling of self.setup().

    :param socket request: The inbound TCPServer request socket.
    :param tuple client_address: The remote client socket address tuple (host, port).
    :param TCPServer server: The parent TCPServer instance.
    """
    self.request = request
    self.client_address = client_address
    self.server = server
    self.logger = logging.getLogger(__name__)

  def handle_request(self):
    """Handle a request (the equivalent of the latter half of BaseRequestHandler.__init__()).

    This is invoked by a TCPServer subclass that spawns a thread from process_request() that invokes
    an overridden process_request_thread().
    """
    self.setup()
    try:
      self.handle()
    finally:
      self.finish()

  def handle(self):
    """Main request handler entrypoint for subclasses."""

  def handle_error(self, exc):
    """Main error handler entrypoint for subclasses."""


class PailgunHandler(PailgunHandlerBase):
  """A nailgun protocol handler for use with forking, SocketServer-based servers."""

  def _run_pants(self, sock, arguments, environment):
    """Execute a given run with a pants runner."""
    self.server.runner_factory(sock, arguments, environment).run()

  def handle(self):
    """Request handler for a single Pailgun request."""
    # Parse the Nailgun request portion.
    _, _, arguments, environment = NailgunProtocol.parse_request(self.request)

    # N.B. the first and second nailgun request arguments (working_dir and command) are currently
    # ignored in favor of a get_buildroot() call within LocalPantsRunner.run() and an assumption
    # that anyone connecting to this nailgun server always intends to run pants itself.

    # Prepend the command to our arguments so it aligns with the expected sys.argv format of python
    # (e.g. [list', '::'] -> ['./pants', 'list', '::']).
    arguments.insert(0, './pants')

    self.logger.info('handling pailgun request: `{}`'.format(' '.join(arguments)))
    self.logger.debug('pailgun request environment: %s', environment)

    # Execute the requested command with optional daemon-side profiling.
    with maybe_profiled(environment.get('PANTSD_PROFILE')):
      self._run_pants(self.request, arguments, environment)

    # NB: This represents the end of pantsd's involvement in the request, but the request will
    # continue to run post-fork.
    self.logger.info('pailgun request completed: `{}`'.format(' '.join(arguments)))

  def handle_error(self, exc=None):
    """Error handler for failed calls to handle()."""
    if exc:
      NailgunProtocol.send_stderr(self.request, traceback.format_exc())
    failure_code = 1
    NailgunProtocol.send_exit_with_code(self.request, failure_code)


class PailgunServer(ThreadingMixIn, TCPServer):
  """A pants nailgun server.

  This class spawns a thread per request via `ThreadingMixIn`: the thread body runs
  `process_request_thread`, which we override.
  """

  timeout = 0.05
  # Override the ThreadingMixIn default, to minimize the chances of zombie pailgun processes.
  daemon_threads = True

  def __init__(self, server_address, runner_factory, lifecycle_lock, request_complete_callback,
               handler_class=None, bind_and_activate=True):
    """Override of TCPServer.__init__().

    N.B. the majority of this function is copied verbatim from TCPServer.__init__().

    :param tuple server_address: An address tuple of (hostname, port) for socket.bind().
    :param class runner_factory: A factory function for creating a DaemonPantsRunner for each run.
    :param threading.RLock lifecycle_lock: A lock used to guard against abrupt teardown of the servers
                                           execution thread during handling. All pailgun request handling
                                           will take place under care of this lock, which would be shared with
                                           a `PailgunServer`-external lifecycle manager to guard teardown.
    :param function request_complete_callback: A callback that will be called whenever a pailgun request is completed.
    :param class handler_class: The request handler class to use for each request. (Optional)
    :param bool bind_and_activate: If True, binds and activates networking at __init__ time.
                                   (Optional)
    """
    # Old-style class, so we must invoke __init__() this way.
    BaseServer.__init__(self, server_address, handler_class or PailgunHandler)
    self.socket = RecvBufferedSocket(socket.socket(self.address_family, self.socket_type))
    self.runner_factory = runner_factory
    self.lifecycle_lock = lifecycle_lock
    self.allow_reuse_address = True           # Allow quick reuse of TCP_WAIT sockets.
    self.server_port = None                   # Set during server_bind() once the port is bound.
    self.request_complete_callback = request_complete_callback

    if bind_and_activate:
      try:
        self.server_bind()
        self.server_activate()
      except Exception:
        self.server_close()
        raise

  def server_bind(self):
    """Override of TCPServer.server_bind() that tracks bind-time assigned random ports."""
    TCPServer.server_bind(self)
    _, self.server_port = self.socket.getsockname()[:2]

  def process_request(self, request, client_address):
    """Start a new thread to process the request.

    This is lovingly copied and pasted from ThreadingMixIn, with the addition of setting the name
    of the thread. It's a shame that ThreadingMixIn doesn't provide a customization hook.
    """
    t = threading.Thread(
      target=self.process_request_thread,
      args=(request, client_address),
      name="PailgunRequestThread",
    )
    t.daemon = self.daemon_threads
    t.start()

  def handle_request(self):
    """Override of TCPServer.handle_request() that provides locking.

    Calling this method has the effect of "maybe" (if the socket does not time out first)
    accepting a request and (because we mixin in ThreadingMixIn) spawning it on a thread. It should
    always return within `min(self.timeout, socket.gettimeout())`.

    N.B. Most of this is copied verbatim from SocketServer.py in the stdlib.
    """
    timeout = self.socket.gettimeout()
    if timeout is None:
      timeout = self.timeout
    elif self.timeout is not None:
      timeout = min(timeout, self.timeout)
    fd_sets = safe_select([self], [], [], timeout)
    if not fd_sets[0]:
      self.handle_timeout()
      return

    # After select tells us we can safely accept, guard the accept and request
    # handling with the lifecycle lock to avoid abrupt teardown mid-request.
    with self.lifecycle_lock():
      self._handle_request_noblock()

  def process_request_thread(self, request, client_address):
    """Override of ThreadingMixIn.process_request_thread() that delegates to the request handler."""
    # Instantiate the request handler.
    Native().override_thread_logging_destination_to_just_stderr()
    handler = self.RequestHandlerClass(request, client_address, self)
    try:
      # Attempt to handle a request with the handler.
      handler.handle_request()
      self.request_complete_callback()

    except Exception as e:
      # If that fails, (synchronously) handle the error with the error handler sans-fork.
      try:
        handler.handle_error(e)
      finally:
        # Shutdown the socket since we don't expect a fork() in the exception context.
        self.shutdown_request(request)
    else:
      # At this point, we expect a fork() has taken place - the parent side will return, and so we
      # close the request here from the parent without explicitly shutting down the socket. The
      # child half of this will perform an os._exit() before it gets to this point and is also
      # responsible for shutdown and closing of the socket when its execution is complete.
      self.close_request(request)
