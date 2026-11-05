use zed_extension_api::{self as zed, settings::LspSettings};
mod distribution;
mod identity;
use std::path::Path;

struct ArgoCD;
struct ZedHost<'a> {
    id: &'a zed::LanguageServerId,
}
impl distribution::Host for ZedHost<'_> {
    fn download(&mut self, url: &str, destination: &Path) -> Result<(), String> {
        zed::set_language_server_installation_status(
            self.id,
            &zed::LanguageServerInstallationStatus::Downloading,
        );
        zed::download_file(
            url,
            destination.to_str().ok_or("Cache path is not UTF-8")?,
            zed::DownloadedFileType::Uncompressed,
        )
    }
    fn executable(&mut self, path: &Path) -> Result<(), String> {
        zed::make_file_executable(path.to_str().ok_or("Cache path is not UTF-8")?)
    }
    fn identity(&mut self, path: &Path, version: &str) -> Result<(), String> {
        let bytes = read_identity(path.to_str().ok_or("Cache path is not UTF-8")?)?;
        identity::validate_release(&bytes, version)
    }
}
fn read_identity(path: &str) -> Result<Vec<u8>, String> {
    let output = zed::process::Command::new(path)
        .args(["version", "--json"])
        .output()
        .map_err(|e| format!("Cannot execute Argo CD server at {path:?}: {e}"))?;
    if output.status != Some(0) {
        return Err("Server identity command failed; check native platform compatibility and executable permissions".into());
    }
    Ok(output.stdout)
}
impl zed::Extension for ArgoCD {
    fn new() -> Self {
        Self
    }
    fn language_server_workspace_configuration(
        &mut self,
        id: &zed::LanguageServerId,
        worktree: &zed::Worktree,
    ) -> zed::Result<Option<serde_json::Value>> {
        if id.as_ref() != "argocd-ls" {
            return Err("Unsupported language server ID".into());
        }
        Ok(LspSettings::for_worktree("argocd-ls", worktree)?.settings)
    }
    fn language_server_command(
        &mut self,
        id: &zed::LanguageServerId,
        worktree: &zed::Worktree,
    ) -> zed::Result<zed::Command> {
        if id.as_ref() != "argocd-ls" {
            return Err("Unsupported language server ID".into());
        }
        let settings = LspSettings::for_worktree("argocd-ls", worktree)?;
        let override_path = settings.settings.as_ref().and_then(|s| s.get("serverPath"));
        let (os, arch) = zed::current_platform();
        // Explicit user configuration always wins, including invalid values.
        let found = if override_path.is_none() {
            worktree.which("argocd-ls")
        } else {
            None
        };
        let local = distribution::local_path(override_path, found, matches!(os, zed::Os::Windows))?;
        let path = match local {
            Some(path) => {
                let bytes = read_identity(&path)
                    .map_err(|e| format!("{e}. Correct settings.serverPath or your shell PATH."))?;
                identity::validate(&bytes).map_err(|e| format!(
                    "Server at {path:?} failed identity validation: {e}. Correct settings.serverPath or your shell PATH."
                ))?;
                path
            }
            None => {
                let os = match os {
                    zed::Os::Linux => "linux",
                    zed::Os::Mac => "macos",
                    zed::Os::Windows => "windows",
                };
                let arch = match arch {
                    zed::Architecture::X8664 => "x86_64",
                    zed::Architecture::Aarch64 => "aarch64",
                    _ => "unsupported",
                };
                let target = distribution::Target::resolve(os, arch)?;
                let root = std::env::current_dir().map_err(|e| e.to_string())?;
                let result = distribution::install(
                    &mut ZedHost { id },
                    &root,
                    target,
                    env!("CARGO_PKG_VERSION"),
                );
                match result {
                    Ok(path) => path.to_str().ok_or("Cache path is not UTF-8")?.to_owned(),
                    Err(error) => {
                        zed::set_language_server_installation_status(
                            id,
                            &zed::LanguageServerInstallationStatus::Failed(error.clone()),
                        );
                        return Err(error);
                    }
                }
            }
        };
        zed::set_language_server_installation_status(
            id,
            &zed::LanguageServerInstallationStatus::None,
        );
        Ok(zed::Command {
            command: path,
            args: vec!["serve".into(), "--stdio".into()],
            env: Vec::new(),
        })
    }
}
zed::register_extension!(ArgoCD);
