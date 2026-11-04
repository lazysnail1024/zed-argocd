use zed_extension_api::{self as zed, settings::LspSettings};
mod identity;
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
        let path = settings.settings.as_ref().and_then(|s| s.get("serverPath")).and_then(|v| v.as_str()).ok_or("Release downloads are not available. Set lsp.argocd-ls.settings.serverPath to an absolute local server path.")?.to_string();
        let (os, _) = zed::current_platform();
        let absolute = match os {
            zed::Os::Windows => path.starts_with("\\\\") || (path.len()>2 && path.as_bytes()[1]==b':' && matches!(path.as_bytes()[2],b'/'|b'\\')),
            _ => path.starts_with('/'),
        };
        if !absolute { return Err("Server binary path must be absolute".into()); }
        let args=vec!["serve".to_string(),"--stdio".to_string()];
        let env = Vec::<(String, String)>::new();
        let output = zed::process::Command::new(&path).args(["version","--json"]).envs(env.clone()).output()?;
        if output.status != Some(0) { return Err("Server identity command failed".into()); }
        identity::validate(&output.stdout)?;
        Ok(zed::Command { command: path, args, env })
    }
}
zed::register_extension!(ArgoCD);
