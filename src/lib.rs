use zed_extension_api::{self as zed, settings::LspSettings};
mod identity;
#[cfg(feature = "bundled-server")]
mod bundled;
struct ArgoCD;
impl zed::Extension for ArgoCD {
    fn new() -> Self { Self }
    fn language_server_workspace_configuration(&mut self, id: &zed::LanguageServerId, worktree: &zed::Worktree) -> zed::Result<Option<serde_json::Value>> {
        if id.as_ref() != "argocd-ls" { return Err("Unsupported language server ID".into()); }
        Ok(LspSettings::for_worktree("argocd-ls", worktree)?.settings)
    }
    fn language_server_command(&mut self, id: &zed::LanguageServerId, worktree: &zed::Worktree) -> zed::Result<zed::Command> {
        if id.as_ref() != "argocd-ls" { return Err("Unsupported language server ID".into()); }
        let settings = LspSettings::for_worktree("argocd-ls", worktree)?;
        let override_path = settings.settings.as_ref().and_then(|s| s.get("serverPath"));
        let path = if let Some(value) = override_path {
            let path = value.as_str().ok_or("serverPath must be an absolute path string")?;
            let (os, _) = zed::current_platform();
            let absolute = match os {
                zed::Os::Windows => path.starts_with("\\\\") || (path.len()>2 && path.as_bytes()[1]==b':' && matches!(path.as_bytes()[2],b'/'|b'\\')),
                _ => path.starts_with('/'),
            };
            if !absolute { return Err("Server binary path must be absolute".into()); }
            path.to_string()
        } else {
            #[cfg(feature = "bundled-server")]
            { bundled::server_path()? }
            #[cfg(not(feature = "bundled-server"))]
            { return Err("This source checkout has no bundled native server. Use a precompiled development bundle or configure settings.serverPath for a contributor build.".into()); }
        };
        let args=vec!["serve".to_string(),"--stdio".to_string()];
        let env = Vec::<(String, String)>::new();
        let output = zed::process::Command::new(&path).args(["version","--json"]).envs(env.clone()).output().map_err(|error| {
            if override_path.is_some() {
                format!("Could not start settings.serverPath {:?}: {}. Correct this absolute path or remove settings.serverPath to use the bundled server.", path, error)
            } else {
                format!("Could not start bundled server payload {:?}: {}. Reinstall the verified development bundle.", path, error)
            }
        })?;
        if output.status != Some(0) { return Err("Server identity command failed".into()); }
        identity::validate(&output.stdout)?;
        Ok(zed::Command { command: path, args, env })
    }
}
zed::register_extension!(ArgoCD);
