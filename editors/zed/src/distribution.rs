//! Verified, version-pinned installation inside Zed's extension work directory.
//!
//! The host seam is also used by the filesystem/download failure tests. Production
//! network and process operations are supplied exclusively by the Zed API.
use sha2::{Digest, Sha256};
use std::{
    fs,
    io::Read,
    path::{Path, PathBuf},
    time::{SystemTime, UNIX_EPOCH},
};

pub fn server_version() -> &'static str {
    include_str!("../native-server-version").trim()
}

fn valid_release_version(version: &str) -> bool {
    let (core, prerelease) = version
        .split_once('-')
        .map_or((version, None), |(core, pre)| (core, Some(pre)));
    let number = |part: &str| {
        !part.is_empty()
            && part.bytes().all(|byte| byte.is_ascii_digit())
            && (part == "0" || !part.starts_with('0'))
    };
    let mut components = core.split('.');
    let core_valid =
        (0..3).all(|_| components.next().is_some_and(number)) && components.next().is_none();
    core_valid
        && prerelease.is_none_or(|pre| {
            pre.split('.').all(|part| {
                !part.is_empty()
                    && part
                        .bytes()
                        .all(|byte| byte.is_ascii_alphanumeric() || byte == b'-')
                    && (!part.bytes().all(|byte| byte.is_ascii_digit()) || number(part))
            })
        })
}

const MAX_BINARY: u64 = 128 * 1024 * 1024;
const MAX_SUMS: u64 = 64 * 1024;
const REPOSITORY: &str = "lazysnail1024/zed-argocd";

pub trait Host {
    fn download(&mut self, url: &str, destination: &Path) -> Result<(), String>;
    fn executable(&mut self, path: &Path) -> Result<(), String>;
    fn identity(&mut self, path: &Path, version: &str) -> Result<(), String>;
    /// Atomically publish a complete nonempty directory. The default is the
    /// same filesystem operation on the native test hosts and in Zed's WASI
    /// sandbox. Tests interleave another installer immediately before it.
    fn publish_directory(&mut self, staging: &Path, destination: &Path) -> std::io::Result<()> {
        fs::rename(staging, destination)
    }
}

#[derive(Clone, Copy, Debug)]
pub struct Target {
    pub platform: &'static str,
    pub executable: &'static str,
}
impl Target {
    pub fn resolve(os: &str, arch: &str) -> Result<Self, String> {
        let platform = match (os, arch) {
            ("linux", "x86_64") => "linux-amd64",
            ("linux", "aarch64") => "linux-arm64",
            ("macos", "x86_64") => "darwin-amd64",
            ("macos", "aarch64") => "darwin-arm64",
            ("windows", "x86_64") => "windows-amd64",
            _ => return Err(format!("No managed Argo CD server is published for {os}/{arch}. Configure settings.serverPath with a compatible native build.")),
        };
        Ok(Self {
            platform,
            executable: if os == "windows" {
                "argocd-ls.exe"
            } else {
                "argocd-ls"
            },
        })
    }
    pub fn asset(&self, version: &str) -> String {
        format!(
            "argocd-ls-{version}-{}{}",
            self.platform,
            if self.executable.ends_with(".exe") {
                ".exe"
            } else {
                ""
            }
        )
    }
}

pub fn local_path(
    value: Option<&serde_json::Value>,
    found: Option<String>,
    windows: bool,
) -> Result<Option<String>, String> {
    let path = match value {
        Some(v) => Some(
            v.as_str()
                .ok_or("settings.serverPath must be an absolute path string")?
                .to_owned(),
        ),
        None => found,
    };
    if let Some(path) = &path {
        let absolute = if windows {
            path.starts_with("\\\\")
                || path.starts_with("//")
                || (path.as_bytes().first().is_some_and(u8::is_ascii_alphabetic)
                    && path.as_bytes().get(1) == Some(&b':')
                    && matches!(path.as_bytes().get(2), Some(b'/' | b'\\')))
        } else {
            path.starts_with('/')
        };
        if !absolute {
            return Err("Server executable path must be absolute; correct settings.serverPath or your shell PATH".into());
        }
    }
    Ok(path)
}

pub fn checksum(sums: &[u8], asset: &str) -> Result<String, String> {
    let text = std::str::from_utf8(sums).map_err(|_| "SHA256SUMS is not UTF-8")?;
    let mut selected = None;
    let mut names = std::collections::HashSet::new();
    for line in text.lines() {
        let (hash, name) = line.split_once("  ").ok_or("Malformed SHA256SUMS entry")?;
        if hash.len() != 64
            || !hash
                .bytes()
                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
            || name.is_empty()
            || !name
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || b"._-".contains(&b))
            || !names.insert(name)
        {
            return Err("Invalid or duplicate SHA256SUMS entry".into());
        }
        if name == asset {
            selected = Some(hash.to_owned());
        }
    }
    selected.ok_or_else(|| format!("SHA256SUMS does not contain {asset}"))
}

fn regular(path: &Path, limit: u64) -> Result<fs::Metadata, String> {
    let meta = fs::symlink_metadata(path)
        .map_err(|e| format!("Cannot inspect {}: {e}", path.display()))?;
    if !meta.file_type().is_file()
        || meta.file_type().is_symlink()
        || meta.len() == 0
        || meta.len() > limit
    {
        return Err(format!(
            "Expected a bounded nonempty regular file: {}",
            path.display()
        ));
    }
    Ok(meta)
}
fn read_sums(path: &Path) -> Result<Vec<u8>, String> {
    regular(path, MAX_SUMS)?;
    let mut bytes = Vec::new();
    fs::File::open(path)
        .and_then(|f| f.take(MAX_SUMS + 1).read_to_end(&mut bytes))
        .map_err(|e| e.to_string())?;
    if bytes.len() as u64 > MAX_SUMS {
        return Err("SHA256SUMS exceeds its size limit".into());
    }
    Ok(bytes)
}
fn verify_bytes(path: &Path, expected: &str) -> Result<(), String> {
    regular(path, MAX_BINARY)?;
    let mut file = fs::File::open(path).map_err(|e| e.to_string())?;
    let mut hash = Sha256::new();
    let mut total = 0_u64;
    let mut buffer = [0_u8; 64 * 1024];
    loop {
        let count = file.read(&mut buffer).map_err(|e| e.to_string())?;
        if count == 0 {
            break;
        }
        total += count as u64;
        if total > MAX_BINARY {
            return Err("Server download exceeds its size limit".into());
        }
        hash.update(&buffer[..count]);
    }
    if format!("{:x}", hash.finalize()) != expected {
        return Err("Server SHA-256 mismatch; refusing to execute downloaded bytes".into());
    }
    Ok(())
}
fn directory(path: &Path, create: bool) -> Result<(), String> {
    match fs::symlink_metadata(path) {
        Ok(meta) if meta.file_type().is_dir() && !meta.file_type().is_symlink() => Ok(()),
        Ok(_) => Err(format!(
            "Cache path must be a real directory: {}",
            path.display()
        )),
        Err(e) if create && e.kind() == std::io::ErrorKind::NotFound => {
            match fs::create_dir(path) {
                Ok(()) => Ok(()),
                Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists => directory(path, false),
                Err(e) => Err(e.to_string()),
            }
        }
        Err(e) => Err(format!(
            "Cannot inspect cache directory {}: {e}",
            path.display()
        )),
    }
}
fn verify<H: Host>(
    host: &mut H,
    dir: &Path,
    target: Target,
    version: &str,
) -> Result<PathBuf, String> {
    directory(dir, false)?;
    let hash = checksum(&read_sums(&dir.join("SHA256SUMS"))?, &target.asset(version))?;
    let binary = dir.join(target.executable);
    verify_bytes(&binary, &hash)?;
    host.executable(&binary)?;
    host.identity(&binary, version)?;
    Ok(binary)
}

fn generation_id(name: &str, slot_name: &str) -> Option<(u128, u32)> {
    let suffix = name.strip_prefix(&format!("{slot_name}.gen-"))?;
    let (stamp, sequence) = suffix.split_once('-')?;
    if stamp.is_empty()
        || sequence.is_empty()
        || !stamp.bytes().all(|b| b.is_ascii_digit())
        || !sequence.bytes().all(|b| b.is_ascii_digit())
    {
        return None;
    }
    Some((stamp.parse().ok()?, sequence.parse().ok()?))
}

fn cached_server<H: Host>(
    host: &mut H,
    cache: &Path,
    slot_name: &str,
    target: Target,
    version: &str,
) -> Result<PathBuf, String> {
    // Preserve an already installed legacy slot without migrating or renaming
    // it. New publications only use immutable, version/platform-scoped siblings.
    let mut last_error = match verify(host, &cache.join(slot_name), target, version) {
        Ok(binary) => return Ok(binary),
        Err(error) => error,
    };
    let mut generations = Vec::new();
    for entry in fs::read_dir(cache).map_err(|e| format!("Cannot list server cache: {e}"))? {
        let entry = entry.map_err(|e| format!("Cannot inspect server cache entry: {e}"))?;
        let name = entry.file_name();
        if let Some(id) = name
            .to_str()
            .and_then(|name| generation_id(name, slot_name))
        {
            generations.push((id, entry.path()));
        }
    }
    generations.sort_by(|a, b| b.cmp(a));
    for (_, path) in generations {
        match verify(host, &path, target, version) {
            Ok(binary) => return Ok(binary),
            Err(error) => last_error = error,
        }
    }
    Err(last_error)
}

struct Staging {
    path: PathBuf,
    stamp: u128,
    sequence: u32,
    owned: bool,
}

fn reserve_staging(cache: &Path) -> Result<Staging, String> {
    let stamp = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_err(|e| e.to_string())?
        .as_nanos();
    for sequence in 0..16 {
        let path = cache.join(format!(".stage-{stamp}-{sequence}"));
        match fs::create_dir(&path) {
            Ok(()) => {
                return Ok(Staging {
                    path,
                    stamp,
                    sequence,
                    owned: true,
                })
            }
            Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => continue,
            Err(error) => return Err(format!("Cannot create download staging directory: {error}")),
        }
    }
    Err("Cannot reserve an isolated download staging directory".into())
}

/// `root` must be the extension's work directory, never a workspace directory.
/// A new version gets its own namespace; failure never selects an older version.
pub fn install<H: Host>(
    host: &mut H,
    root: &Path,
    target: Target,
    version: &str,
) -> Result<PathBuf, String> {
    if !valid_release_version(version) {
        return Err("Invalid pinned server version".into());
    }
    let cache = root.join("servers");
    directory(&cache, true)?;
    let slot_name = format!("v{version}-{}", target.platform);
    let legacy = cache.join(&slot_name);
    // Preserve the existing refusal of unsafe legacy directory entries. Cache
    // candidates are always inspected without following directory symlinks.
    if fs::symlink_metadata(&legacy).is_ok() {
        directory(&legacy, false)?;
    }
    let previous_error = match cached_server(host, &cache, &slot_name, target, version) {
        Ok(binary) => return Ok(binary),
        Err(e) => e,
    };
    let mut staging = reserve_staging(&cache)?;
    let result = download_and_install(host, &mut staging, &cache, &slot_name, target, version);
    // A successful rename transfers ownership to the published cache. Never
    // touch its former staging pathname: another process may already reuse it.
    let cleanup = if staging.owned {
        fs::remove_dir_all(&staging.path)
            .err()
            .filter(|e| e.kind() != std::io::ErrorKind::NotFound)
    } else {
        None
    };
    if let Err(error) = result {
        return Err(format!("Cannot install Argo CD server v{version} for {}: {error}. Cached server was unavailable: {previous_error}. Retry with network access after a release is published, or configure settings.serverPath.{}", target.platform, cleanup.map(|e| format!(" Staging cleanup failed: {e}")).unwrap_or_default()));
    }
    if let Some(error) = cleanup {
        eprintln!("Argo CD: verified install ready; redundant download cleanup deferred: {error}");
    }
    result
}
fn download_and_install<H: Host>(
    host: &mut H,
    staging: &mut Staging,
    cache: &Path,
    slot_name: &str,
    target: Target,
    version: &str,
) -> Result<PathBuf, String> {
    let base = format!("https://github.com/{REPOSITORY}/releases/download/v{version}");
    let sums_url = format!("{base}/SHA256SUMS");
    host.download(&sums_url, &staging.path.join("SHA256SUMS"))
        .map_err(|error| format!("Cannot download checksum manifest from {sums_url}: {error}"))?;
    directory(&staging.path, false)?;
    let hash = checksum(
        &read_sums(&staging.path.join("SHA256SUMS"))?,
        &target.asset(version),
    )?;
    let binary = staging.path.join(target.executable);
    let binary_url = format!("{base}/{}", target.asset(version));
    host.download(&binary_url, &binary)
        .map_err(|error| format!("Cannot download native server from {binary_url}: {error}"))?;
    directory(&staging.path, false)?;
    verify_bytes(&binary, &hash)?;
    host.executable(&binary)?;
    host.identity(&binary, version)?;
    if let Ok(winner) = cached_server(host, cache, slot_name, target, version) {
        return Ok(winner);
    }

    // This lookup is only an optimization, never a lock. Another installer can
    // publish immediately after it. Every new destination is an immutable
    // generation; no published directory is moved, replaced, or removed.
    // A complete nonempty directory cannot be replaced by directory rename on
    // Unix or Windows. Handle all collision error kinds by inspecting the
    // destination, since Windows can report PermissionDenied for this case.
    for attempt in 0..16 {
        let destination = cache.join(format!(
            "{slot_name}.gen-{}-{}",
            staging.stamp,
            staging.sequence + attempt
        ));
        match fs::symlink_metadata(&destination) {
            Ok(_) => {
                if let Ok(winner) = verify(host, &destination, target, version) {
                    return Ok(winner);
                }
                continue;
            }
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
            Err(error) => return Err(format!("Cannot inspect generation destination: {error}")),
        }
        match host.publish_directory(&staging.path, &destination) {
            Ok(()) => {
                staging.owned = false;
                return Ok(destination.join(target.executable));
            }
            Err(error) => {
                if let Ok(winner) = verify(host, &destination, target, version) {
                    return Ok(winner);
                }
                if let Ok(winner) = cached_server(host, cache, slot_name, target, version) {
                    return Ok(winner);
                }
                match fs::symlink_metadata(&destination) {
                    Ok(_) => continue, // Preserve even an invalid collided entry.
                    Err(inspect) if inspect.kind() == std::io::ErrorKind::NotFound => {
                        return Err(format!("Cannot publish verified server generation: {error}"));
                    }
                    Err(inspect) => return Err(format!("Cannot publish verified server generation: {error}; cannot inspect destination: {inspect}")),
                }
            }
        }
    }
    Err("Cannot find an unused server generation destination; retry installation".into())
}
