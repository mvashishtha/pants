# Copyright 2022 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).
from __future__ import annotations

import dataclasses
import os
import textwrap
from typing import Optional

from pants.backend.python.subsystems.debugpy import DebugPy
from pants.backend.python.target_types import (
    ConsoleScript,
    PexEntryPointField,
    ResolvedPexEntryPoint,
    ResolvePexEntryPointRequest,
)
from pants.backend.python.util_rules.interpreter_constraints import InterpreterConstraints
from pants.backend.python.util_rules.local_dists import LocalDistsPex, LocalDistsPexRequest
from pants.backend.python.util_rules.pex import Pex, PexRequest, VenvPex, VenvPexRequest
from pants.backend.python.util_rules.pex_environment import PexEnvironment
from pants.backend.python.util_rules.pex_from_targets import (
    InterpreterConstraintsRequest,
    PexFromTargetsRequest,
)
from pants.backend.python.util_rules.python_sources import (
    PythonSourceFiles,
    PythonSourceFilesRequest,
)
from pants.core.goals.run import RunDebugAdapterRequest, RunRequest
from pants.core.subsystems.debug_adapter import DebugAdapterSubsystem
from pants.engine.addresses import Address
from pants.engine.fs import CreateDigest, Digest, FileContent, MergeDigests
from pants.engine.rules import Get, MultiGet, rule_helper
from pants.engine.target import TransitiveTargets, TransitiveTargetsRequest


def _in_chroot(relpath: str) -> str:
    return os.path.join("{chroot}", relpath)


@rule_helper
async def _create_python_source_run_request(
    address: Address,
    *,
    entry_point_field: PexEntryPointField,
    pex_env: PexEnvironment,
    run_in_sandbox: bool,
    console_script: Optional[ConsoleScript] = None,
) -> RunRequest:
    addresses = [address]
    interpreter_constraints, entry_point, transitive_targets = await MultiGet(
        Get(InterpreterConstraints, InterpreterConstraintsRequest(addresses)),
        Get(
            ResolvedPexEntryPoint,
            ResolvePexEntryPointRequest(entry_point_field),
        ),
        Get(TransitiveTargets, TransitiveTargetsRequest(addresses)),
    )

    pex_filename = (
        address.generated_name.replace(".", "_") if address.generated_name else address.target_name
    )

    pex_request, sources = await MultiGet(
        Get(
            PexRequest,
            PexFromTargetsRequest(
                addresses,
                output_filename=f"{pex_filename}.pex",
                internal_only=True,
                include_source_files=False,
                # `PEX_EXTRA_SYS_PATH` should contain this entry_point's module.
                main=console_script or entry_point.val,
                additional_args=(
                    # N.B.: Since we cobble together the runtime environment via PEX_EXTRA_SYS_PATH
                    # below, it's important for any app that re-executes itself that these environment
                    # variables are not stripped.
                    "--no-strip-pex-env",
                ),
            ),
        ),
        Get(
            PythonSourceFiles,
            PythonSourceFilesRequest(transitive_targets.closure, include_files=True),
        ),
    )

    local_dists = await Get(
        LocalDistsPex,
        LocalDistsPexRequest(
            addresses,
            internal_only=True,
            interpreter_constraints=interpreter_constraints,
            sources=sources,
        ),
    )
    pex_request = dataclasses.replace(
        pex_request, pex_path=(*pex_request.pex_path, local_dists.pex)
    )

    complete_pex_environment = pex_env.in_workspace()
    venv_pex = await Get(VenvPex, VenvPexRequest(pex_request, complete_pex_environment))
    input_digests = [
        venv_pex.digest,
        # Note regarding not-in-sandbox mode: You might think that the sources don't need to be copied
        # into the chroot when using inline sources. But they do, because some of them might be
        # codegenned, and those won't exist in the inline source tree. Rather than incurring the
        # complexity of figuring out here which sources were codegenned, we copy everything.
        # The inline source roots precede the chrooted ones in PEX_EXTRA_SYS_PATH, so the inline
        # sources will take precedence and their copies in the chroot will be ignored.
        local_dists.remaining_sources.source_files.snapshot.digest,
    ]
    merged_digest = await Get(Digest, MergeDigests(input_digests))

    chrooted_source_roots = [_in_chroot(sr) for sr in sources.source_roots]
    # The order here is important: we want the in-repo sources to take precedence over their
    # copies in the sandbox (see above for why those copies exist even in non-sandboxed mode).
    source_roots = [
        *([] if run_in_sandbox else sources.source_roots),
        *chrooted_source_roots,
    ]
    extra_env = {
        **complete_pex_environment.environment_dict(python_configured=venv_pex.python is not None),
        "PEX_EXTRA_SYS_PATH": os.pathsep.join(source_roots),
    }

    return RunRequest(
        digest=merged_digest,
        args=[_in_chroot(venv_pex.pex.argv0)],
        extra_env=extra_env,
    )


@rule_helper
async def _create_python_source_run_dap_request(
    regular_run_request: RunRequest,
    *,
    entry_point_field: PexEntryPointField,
    debugpy: DebugPy,
    debug_adapter: DebugAdapterSubsystem,
    console_script: Optional[ConsoleScript] = None,
) -> RunDebugAdapterRequest:
    entry_point, debugpy_pex, launcher_digest = await MultiGet(
        Get(
            ResolvedPexEntryPoint,
            ResolvePexEntryPointRequest(entry_point_field),
        ),
        Get(Pex, PexRequest, debugpy.to_pex_request()),
        Get(
            Digest,
            CreateDigest(
                [
                    FileContent(
                        "__debugpy_launcher.py",
                        textwrap.dedent(
                            """
                            import os
                            CHROOT = os.environ["PANTS_CHROOT"]

                            del os.environ["PEX_INTERPRETER"]

                            # See https://github.com/pantsbuild/pants/issues/17540
                            # For `run --debug-adapter`, the client might send a `pathMappings`
                            # (this is likely as VS Code likes to configure that by default) with
                            # a `remoteRoot` of ".". For `run`, CWD is set to the build root, so
                            # breakpoints set in-repo will never be hit. We fix this by monkeypatching
                            # pydevd (the library powering debugpy) so that a remoteRoot of "."
                            # means the sandbox root.

                            import debugpy._vendored.force_pydevd
                            from _pydevd_bundle.pydevd_process_net_command_json import PyDevJsonCommandProcessor
                            orig_resolve_remote_root = PyDevJsonCommandProcessor._resolve_remote_root

                            def patched_resolve_remote_root(self, local_root, remote_root):
                                if remote_root == ".":
                                    remote_root = CHROOT
                                return orig_resolve_remote_root(self, local_root, remote_root)

                            PyDevJsonCommandProcessor._resolve_remote_root = patched_resolve_remote_root

                            from debugpy.server import cli
                            cli.main()
                            """
                        ).encode("utf-8"),
                    ),
                ]
            ),
        ),
    )

    merged_digest = await Get(
        Digest,
        MergeDigests(
            [
                regular_run_request.digest,
                debugpy_pex.digest,
                launcher_digest,
            ]
        ),
    )
    extra_env = dict(regular_run_request.extra_env)
    extra_env["PEX_PATH"] = os.pathsep.join(
        [
            extra_env["PEX_PATH"],
            # For debugpy to work properly, we need to have just one "environment" for our
            # command to run in. Therefore, we cobble one together with PEX_PATH.
            _in_chroot(debugpy_pex.name),
        ]
    )
    extra_env["PEX_INTERPRETER"] = "1"
    extra_env["PANTS_CHROOT"] = _in_chroot("").rstrip("/")
    main = console_script or entry_point.val
    assert main is not None
    args = [
        *regular_run_request.args,
        _in_chroot("__debugpy_launcher.py"),
        *debugpy.get_args(debug_adapter, main),
    ]

    return RunDebugAdapterRequest(digest=merged_digest, args=args, extra_env=extra_env)
