use serde::Deserialize;
use std::{fs, path::Path};
use zed_extension_api as zed;

#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
struct Manifest {
    schema_version: u32,
    product: String,
    platform: String,
    binary_sha256: String,
    binary_size: usize,
}

pub fn server_path() -> Result<String, String> {
    let bytes = include_bytes!("../native/server.bin");
    let manifest: Manifest = serde_json::from_str(include_str!("../native/manifest.json"))
        .map_err(|e| format!("Invalid bundled server manifest: {e}"))?;
    let (os, arch) = zed::current_platform();
    let platform = match (os, arch) {
        (zed::Os::Linux, zed::Architecture::X8664) => "linux/amd64",
        (zed::Os::Linux, zed::Architecture::Aarch64) => "linux/arm64",
        (zed::Os::Mac, zed::Architecture::X8664) => "darwin/amd64",
        (zed::Os::Mac, zed::Architecture::Aarch64) => "darwin/arm64",
        (zed::Os::Windows, zed::Architecture::X8664) => "windows/amd64",
        _ => return Err("Unsupported bundled server platform".into()),
    };
    if manifest.schema_version != 1 || manifest.product != "argo-cd"
        || manifest.platform != platform || manifest.binary_size != bytes.len()
        || manifest.binary_sha256.len() != 64
        || !manifest.binary_sha256.bytes().all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b)) {
        return Err("Bundled server manifest does not match this platform or payload".into());
    }
    let directory = Path::new("bundled-server");
    match fs::symlink_metadata(directory) {
        Ok(m) if !m.is_dir() || m.file_type().is_symlink() => return Err("Bundled server cache must be a directory".into()),
        Ok(_) => (),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => fs::create_dir(directory).map_err(|e| e.to_string())?,
        Err(e) => return Err(e.to_string()),
    }
    let suffix = if matches!(os, zed::Os::Windows) { ".exe" } else { "" };
    let path = directory.join(format!("argocd-ls-{}{suffix}", manifest.binary_sha256));
    let valid = match fs::symlink_metadata(&path) {
        Ok(m) if !m.is_file() || m.file_type().is_symlink() => return Err("Bundled server cache must be a regular file".into()),
        Ok(m) => m.len() == bytes.len() as u64 && fs::read(&path).map_err(|e| e.to_string())? == bytes,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => false,
        Err(e) => return Err(e.to_string()),
    };
    if !valid {
        // Remove an old regular file before writing, so hard links cannot redirect writes.
        if path.exists() { fs::remove_file(&path).map_err(|e| e.to_string())?; }
        use std::io::Write;
        let mut file = fs::OpenOptions::new().write(true).create_new(true).open(&path).map_err(|e| e.to_string())?;
        file.write_all(bytes).map_err(|e| e.to_string())?;
    }
    let path = path.to_str().ok_or("Invalid bundled server cache path")?.to_string();
    zed::make_file_executable(&path)?;
    let absolute = std::env::current_dir().map_err(|e| e.to_string())?.join(path);
    Ok(absolute.to_str().ok_or("Invalid absolute bundled server path")?.to_string())
}
