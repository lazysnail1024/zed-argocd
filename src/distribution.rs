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

const MAX_BINARY: u64 = 128 * 1024 * 1024;
const MAX_SUMS: u64 = 64 * 1024;
const REPOSITORY: &str = "lazysnail1024/zed-argocd";

pub trait Host {
    fn download(&mut self, url: &str, destination: &Path) -> Result<(), String>;
    fn executable(&mut self, path: &Path) -> Result<(), String>;
    fn identity(&mut self, path: &Path, version: &str) -> Result<(), String>;
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

fn cleanup_after_success(path: &Path, description: &str) {
    if let Err(error) = fs::remove_dir_all(path) {
        if error.kind() != std::io::ErrorKind::NotFound {
            eprintln!("Argo CD: verified install ready; {description} cleanup deferred: {error}");
        }
    }
}

/// `root` must be the extension's work directory, never a workspace directory.
/// A new version gets its own slot; failure never selects an older version.
pub fn install<H: Host>(
    host: &mut H,
    root: &Path,
    target: Target,
    version: &str,
) -> Result<PathBuf, String> {
    if version.split('.').count() != 3
        || !version
            .split('.')
            .all(|x| !x.is_empty() && x.bytes().all(|b| b.is_ascii_digit()))
    {
        return Err("Invalid pinned server version".into());
    }
    let cache = root.join("servers");
    directory(&cache, true)?;
    let slot = cache.join(format!("v{version}-{}", target.platform));
    let previous_error = match verify(host, &slot, target, version) {
        Ok(binary) => return Ok(binary),
        Err(e) => e,
    };
    // Never rename/remove a symlink or any object outside the managed cache.
    if fs::symlink_metadata(&slot).is_ok() {
        directory(&slot, false)?;
    }
    let stamp = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_err(|e| e.to_string())?
        .as_nanos();
    let mut staging = None;
    for suffix in 0..16 {
        let candidate = cache.join(format!(".stage-{stamp}-{suffix}"));
        match fs::create_dir(&candidate) {
            Ok(()) => {
                staging = Some(candidate);
                break;
            }
            Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists => continue,
            Err(e) => return Err(format!("Cannot create download staging directory: {e}")),
        }
    }
    let staging = staging.ok_or("Cannot reserve an isolated download staging directory")?;
    let result = download_and_install(host, &staging, &slot, target, version);
    if let Err(error) = result {
        let cleanup = fs::remove_dir_all(&staging)
            .err()
            .filter(|e| e.kind() != std::io::ErrorKind::NotFound);
        return Err(format!("Cannot install Argo CD server v{version} for {}: {error}. Cached server was unavailable: {previous_error}. Retry with network access after a release is published, or configure settings.serverPath.{}", target.platform, cleanup.map(|e| format!(" Staging cleanup failed: {e}")).unwrap_or_default()));
    }
    result
}
fn download_and_install<H: Host>(
    host: &mut H,
    staging: &Path,
    slot: &Path,
    target: Target,
    version: &str,
) -> Result<PathBuf, String> {
    let base = format!("https://github.com/{REPOSITORY}/releases/download/v{version}");
    host.download(&format!("{base}/SHA256SUMS"), &staging.join("SHA256SUMS"))?;
    directory(staging, false)?;
    let hash = checksum(
        &read_sums(&staging.join("SHA256SUMS"))?,
        &target.asset(version),
    )?;
    let binary = staging.join(target.executable);
    host.download(&format!("{base}/{}", target.asset(version)), &binary)?;
    directory(staging, false)?;
    verify_bytes(&binary, &hash)?;
    host.executable(&binary)?;
    host.identity(&binary, version)?;
    // A second Zed process may have finished the same pinned install while we
    // downloaded. Reuse its verified winner instead of renaming an executable
    // that may already be running (especially on Windows).
    if let Ok(winner) = verify(host, slot, target, version) {
        cleanup_after_success(staging, "redundant download");
        return Ok(winner);
    }
    // Publish the verified directory as one operation. A failed upgrade leaves
    // other version slots untouched. A broken current slot can be restored if
    // promotion fails; Windows-in-use errors never discard the current slot.
    let backup = staging.with_extension("old");
    let had_slot = fs::symlink_metadata(slot).is_ok();
    if had_slot {
        directory(slot, false)?;
        if let Err(error) = fs::rename(slot, &backup) {
            if let Ok(winner) = verify(host, slot, target, version) {
                cleanup_after_success(staging, "redundant download");
                return Ok(winner);
            }
            return Err(format!(
                "Cannot replace invalid cache (close Zed and retry): {error}"
            ));
        }
    }
    if let Err(e) = fs::rename(staging, slot) {
        if let Ok(winner) = verify(host, slot, target, version) {
            cleanup_after_success(staging, "redundant download");
            if had_slot {
                cleanup_after_success(&backup, "old cache");
            }
            return Ok(winner);
        }
        if had_slot {
            fs::rename(&backup, slot).map_err(|restore| format!("Cache promotion failed: {e}; restore failed: {restore}. Previous cache remains at {}", backup.display()))?;
        }
        return Err(format!("Cannot promote verified server: {e}"));
    }
    if had_slot {
        // A stale quarantine is harmless and must not make a verified install
        // unusable when an antivirus or another process temporarily holds it.
        cleanup_after_success(&backup, "old cache");
    }
    Ok(slot.join(target.executable))
}
