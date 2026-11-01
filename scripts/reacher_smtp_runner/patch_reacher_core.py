"""Apply the audited Hickory patch to an exact Reacher v0.11.11 core tree."""

from __future__ import annotations

import argparse
from collections.abc import Callable
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import sys
import tempfile


UNPATCHED_MANIFEST_SHA256 = "be6a14448932656dae05bd41deb4a21b21d7a3f498b17f5f830cba3475ac2812"
PATCHED_MANIFEST_SHA256 = "8cdbc48d6ea4efec2022ef5b24c165139d44d8a299434ce359086d73c47d299e"
UNPATCHED_MX_SHA256 = "290f536405a6e7483b2f3d7033c80ec7d468ace3dba76e7f8c8e6b473e85f1a7"
PATCHED_MX_SHA256 = "1e720e2b3f5488532983e354fe499d741f8b45081342cc6a06d2c3c54fd7634f"
UNPATCHED_CORE_SHA256 = "aca4e1d0d856dc5ac172791f7347776cac03630d12e9545375e2727bb39cb910"
PATCHED_CORE_SHA256 = "7a7ca3fdae5e3d82e5ebfbc12c99f20cda90c4b812bcd466729c2df3bdf07024"
_OLD_DEPENDENCIES = b'hickory-proto = "0.24.0"\nhickory-resolver = "0.24.0"'
_NEW_DEPENDENCIES = b'hickory-proto = "=0.26.2"\nhickory-resolver = "=0.26.2"'


class ReacherPatchError(RuntimeError):
    """Raised when a core tree is not the reviewed Reacher v0.11.11 source."""


PatchFunction = Callable[[bytes], bytes]


@dataclass(frozen=True)
class _Patch:
    source_hash: str
    patched_hash: str
    transform: PatchFunction


def _patched_manifest_bytes(original: bytes) -> bytes:
    if original.count(_OLD_DEPENDENCIES) != 1:
        raise ReacherPatchError("Reacher hickory dependencies are not patchable")
    return original.replace(_OLD_DEPENDENCIES, _NEW_DEPENDENCIES)


def _patched_mx_bytes(original: bytes) -> bytes:
    patched = original
    for old, new in _MX_REPLACEMENTS:
        if patched.count(old) != 1:
            raise ReacherPatchError("Reacher MX compatibility source is not patchable")
        patched = patched.replace(old, new)
    return patched


def _patched_core_bytes(original: bytes) -> bytes:
    if original.count(b".preference()") != 1 or original.count(b"host.exchange(),") != 1:
        raise ReacherPatchError("Reacher core field access is not patchable")
    patched = original.replace(b".preference()", b".preference")
    patched = patched.replace(b"host.exchange(),", b"&host.exchange,")
    return patched


_MANIFEST_PATCH = _Patch(UNPATCHED_MANIFEST_SHA256, PATCHED_MANIFEST_SHA256, _patched_manifest_bytes)
_MX_PATCH = _Patch(UNPATCHED_MX_SHA256, PATCHED_MX_SHA256, _patched_mx_bytes)
_CORE_PATCH = _Patch(UNPATCHED_CORE_SHA256, PATCHED_CORE_SHA256, _patched_core_bytes)


def _planned_patch(path: Path, patch: _Patch) -> bytes | None:
    try:
        original = path.read_bytes()
    except OSError as exc:
        raise ReacherPatchError("Reacher core source is unavailable") from exc
    digest = hashlib.sha256(original).hexdigest()
    if digest == patch.patched_hash:
        return None
    if digest != patch.source_hash:
        raise ReacherPatchError("Reacher core source does not match v0.11.11")
    patched = patch.transform(original)
    if hashlib.sha256(patched).hexdigest() != patch.patched_hash:
        raise ReacherPatchError("Reacher security patch integrity check failed")
    return patched


def _atomic_replace(path: Path, content: bytes) -> None:
    descriptor = -1
    temporary: str | None = None
    try:
        mode = path.stat().st_mode
        descriptor, temporary = tempfile.mkstemp(prefix=".reacher-core-", dir=path.parent)
        with os.fdopen(descriptor, "wb") as output:
            descriptor = -1
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
        temporary = None
    except OSError as exc:
        raise ReacherPatchError("Reacher core source could not be patched") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary is not None:
            try:
                Path(temporary).unlink()
            except OSError:
                pass


def _apply_patch(path: Path, patch: _Patch) -> bool:
    content = _planned_patch(path, patch)
    if content is None:
        return False
    _atomic_replace(path, content)
    return True


def patch_core_manifest(manifest: Path) -> bool:
    """Pin only the reviewed direct Hickory dependencies."""
    return _apply_patch(manifest, _MANIFEST_PATCH)


def patch_mx_source(mx_source: Path) -> bool:
    """Apply the minimum Hickory 0.26 MX API compatibility patch."""
    return _apply_patch(mx_source, _MX_PATCH)


def patch_core_source(core_source: Path) -> bool:
    """Apply the direct MX field-access adjustment in Reacher's core."""
    return _apply_patch(core_source, _CORE_PATCH)


def patch_reacher_core(manifest: Path) -> bool:
    """Validate all three files before atomically updating any of them."""
    mx_source = manifest.parent / "src" / "mx" / "mod.rs"
    core_source = manifest.parent / "src" / "lib.rs"
    manifest_content = _planned_patch(manifest, _MANIFEST_PATCH)
    mx_content = _planned_patch(mx_source, _MX_PATCH)
    core_content = _planned_patch(core_source, _CORE_PATCH)
    if manifest_content is not None:
        _atomic_replace(manifest, manifest_content)
    if mx_content is not None:
        _atomic_replace(mx_source, mx_content)
    if core_content is not None:
        _atomic_replace(core_source, core_content)
    return manifest_content is not None or mx_content is not None or core_content is not None


def _default_manifest() -> Path:
    return Path(__file__).resolve().parents[2] / ".tools" / "reacher-v0.11.11" / "core" / "Cargo.toml"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, default=_default_manifest())
    args = parser.parse_args()
    try:
        changed = patch_reacher_core(args.manifest)
    except ReacherPatchError as exc:
        parser.error(str(exc))
    sys.stdout.write("patched\n" if changed else "already-patched\n")
    return 0


_MX_REPLACEMENTS = (
    (
        b"""use hickory_proto::rr::rdata::MX;
use hickory_resolver::error::{ResolveError, ResolveErrorKind};
use hickory_resolver::lookup::MxLookup;
use hickory_resolver::system_conf::read_system_conf;
use hickory_resolver::TokioAsyncResolver;
""",
        b"""use hickory_proto::rr::{rdata::MX, RData};

#[cfg(target_vendor = "apple")]
use hickory_resolver::{
	config::{NameServerConfig, ResolverConfig},
	net::runtime::TokioRuntimeProvider,
};
use hickory_resolver::{lookup::Lookup, net::NetError, TokioResolver};
""",
    ),
    (b"pub lookup: Result<MxLookup, ResolveError>,", b"pub lookup: Result<Vec<MX>, NetError>,"),
    (b"lookup: Err(ResolveError::from(\"Skipped\")),", b"lookup: Err(NetError::Message(\"Skipped\")),"),
    (
        b"impl From<MxLookup> for MxDetails {\n\tfn from(lookup: MxLookup) -> Self {",
        b"impl From<Vec<MX>> for MxDetails {\n\tfn from(lookup: Vec<MX>) -> Self {",
    ),
    (b"host.exchange().to_string()", b"host.exchange.to_string()"),
    (b"ResolveError(Box<ResolveError>)", b"ResolveError(Box<NetError>)"),
    (
        b"""impl From<ResolveError> for MxError {
	fn from(e: ResolveError) -> Self {
""",
        b"""impl From<NetError> for MxError {
	fn from(e: NetError) -> Self {
""",
    ),
    (
        b"""\t// Construct a new Resolver with default configuration options
\tlet (config, opts) = read_system_conf()?;
\tlet resolver = TokioAsyncResolver::tokio(config, opts);

\tlet lookup = match resolver.mx_lookup(&syntax.domain).await {
\t\tOk(lookup) => lookup,
\t\tErr(err) => {
\t\t\treturn match err.kind() {
\t\t\t\t// Prefer to return an empty MX lookup if there are no records.
\t\t\t\tResolveErrorKind::NoRecordsFound { .. } => Ok(MxDetails {
\t\t\t\t\tlookup: Err(err),
\t\t\t\t\taccepts_mail: false,
\t\t\t\t}),
\t\t\t\t_ => Err(err.into()),
\t\t\t};
\t\t}
\t};

\t// A null MX explicitly states that the domain does not accept mail.
\tif is_null_mx(lookup.iter()) {
\t\treturn Ok(MxDetails {
\t\t\tlookup: Ok(lookup),
\t\t\taccepts_mail: false,
\t\t});
\t}
""",
        b"""\tlet resolver = resolver()?;

\tlet lookup = match resolver.mx_lookup(&syntax.domain).await {
\t\tOk(lookup) => lookup,
\t\tErr(err) => {
\t\t\tif err.is_no_records_found() {
\t\t\t\treturn Ok(MxDetails {
\t\t\t\t\tlookup: Err(err),
\t\t\t\t\taccepts_mail: false,
\t\t\t\t});
\t\t\t}
\t\t\treturn Err(err.into());
\t\t}
\t};
\tlet records = mx_records(&lookup);

\t// A null MX explicitly states that the domain does not accept mail.
\tif is_null_mx(records.iter()) {
\t\treturn Ok(MxDetails {
\t\t\tlookup: Ok(records),
\t\t\taccepts_mail: false,
\t\t});
\t}
""",
    ),
    (
        b"""\t\tlookup
\t\t\t.iter()
\t\t\t.map(|mx| resolver.lookup_ip(mx.exchange().clone())),
\t)
\t.await;
\tlet accepts_mail = !ip_lookups.iter().all(|res| {
\t\tmatches!(
\t\t\tres.as_ref().map_err(|e| e.kind()),
\t\t\tErr(ResolveErrorKind::NoRecordsFound { .. })
\t\t)
\t});

\tOk(MxDetails {
\t\tlookup: Ok(lookup),
\t\taccepts_mail,
\t})
}

/// Check if the MX records form a null MX (RFC 7505), i.e. records
""",
        b"""\t\trecords
\t\t\t.iter()
\t\t\t.map(|mx| resolver.lookup_ip(mx.exchange.clone())),
\t)
\t.await;
\tlet accepts_mail = !ip_lookups
\t\t.iter()
\t\t.all(|result| result.as_ref().is_err_and(|error| error.is_no_records_found()));

\tOk(MxDetails {
\t\tlookup: Ok(records),
\t\taccepts_mail,
\t})
}

#[cfg(target_vendor = "apple")]
fn resolver() -> Result<TokioResolver, MxError> {
\tlet configuration = std::fs::read_to_string("/etc/resolv.conf")?;
\tlet nameservers = local_nameservers(&configuration);
\tif nameservers.is_empty() {
\t\treturn Err(MxError::IoError(io::Error::new(
\t\t\tio::ErrorKind::NotFound,
\t\t\t"no usable local DNS nameserver",
\t\t)));
\t}
\tTokioResolver::builder_with_config(
\t\tResolverConfig::from_parts(None, vec![], nameservers),
\t\tTokioRuntimeProvider::default(),
\t)
\t.build()
\t.map_err(Into::into)
}

#[cfg(not(target_vendor = "apple"))]
fn resolver() -> Result<TokioResolver, MxError> {
\tOk(TokioResolver::builder_tokio()?.build()?)
}

#[cfg(target_vendor = "apple")]
#[doc(hidden)]
pub fn local_nameservers(configuration: &str) -> Vec<NameServerConfig> {
\tconfiguration
\t\t.lines()
\t\t.filter_map(|line| {
\t\t\tlet mut fields = line.split_whitespace();
\t\t\tmatch (fields.next(), fields.next(), fields.next()) {
\t\t\t\t(Some("nameserver"), Some(address), None) => address.parse().ok(),
\t\t\t\t_ => None,
\t\t\t}
\t\t})
\t\t.map(NameServerConfig::udp_and_tcp)
\t\t.collect()
}

fn mx_records(lookup: &Lookup) -> Vec<MX> {
\tlookup
\t\t.answers()
\t\t.iter()
\t\t.filter_map(|record| match &record.data {
\t\t\tRData::MX(mx) => Some(mx.clone()),
\t\t\t_ => None,
\t\t})
\t\t.collect()
}

/// Check if the MX records form a null MX (RFC 7505), i.e. records
""",
    ),
    (b"mx.exchange().is_root()", b"mx.exchange.is_root()"),
    (
        b"""\t#[test]
\tfn non_microsoft_hosts() {
\t\tfor host in ["gmail-smtp-in.l.google.com.", "mx.microsoft.example.com."] {
\t\t\tassert!(!is_hotmail(host), "{}", host);
\t\t\tassert!(!is_hotmail_b2c(host), "{}", host);
\t\t\tassert!(!is_hotmail_b2b(host), "{}", host);
\t\t}
\t}
}
""",
        b"""\t#[test]
\tfn non_microsoft_hosts() {
\t\tfor host in ["gmail-smtp-in.l.google.com.", "mx.microsoft.example.com."] {
\t\t\tassert!(!is_hotmail(host), "{}", host);
\t\t\tassert!(!is_hotmail_b2c(host), "{}", host);
\t\t\tassert!(!is_hotmail_b2b(host), "{}", host);
\t\t}
\t}

\t#[test]
\tfn local_dns_config_ignores_scoped_ipv6_addresses() {
\t\tlet nameservers = local_nameservers("nameserver fe80::1%en0\\nnameserver 192.0.2.1\\n");
\t\tassert_eq!(nameservers.len(), 1);
\t\tassert_eq!(nameservers[0].ip.to_string(), "192.0.2.1");
\t}
}
""",
    ),
)


if __name__ == "__main__":
    raise SystemExit(main())
