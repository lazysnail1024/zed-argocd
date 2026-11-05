use serde::Deserialize;

#[derive(Deserialize)]
struct Identity {
    product: String,
    #[serde(rename = "serverVersion")]
    server_version: String,
    #[serde(rename = "adapterContractVersion")]
    contract: u32,
    #[serde(rename = "analysisReady")]
    ready: bool,
    #[serde(rename = "configSchemaVersions")]
    configs: Vec<u32>,
    #[serde(rename = "reportSchemaVersions")]
    reports: Vec<u32>,
    #[serde(rename = "toolkitVersion")]
    toolkit: Option<String>,
    #[serde(rename = "catalogHash")]
    catalog: Option<String>,
    #[serde(rename = "sourceCommit")]
    _commit: serde_json::Value,
    dirty: bool,
    #[serde(rename = "buildMode")]
    mode: String,
}

pub fn validate(bytes: &[u8]) -> Result<(), String> {
    let id: Identity =
        serde_json::from_slice(bytes).map_err(|e| format!("Invalid server identity: {e}"))?;
    if id.product != "argo-cd" {
        return Err("Expected argo-cd server product".into());
    }
    if id.contract != 1 {
        return Err("Unsupported adapter contract".into());
    }
    if !id.ready {
        return Err("Argo CD server analysis is not ready".into());
    }
    if id.server_version.is_empty()
        || !id.configs.contains(&1)
        || !id.reports.contains(&1)
        || id.toolkit.as_deref().unwrap_or("").is_empty()
        || !id
            .catalog
            .as_deref()
            .is_some_and(|s| s.len() == 64 && s.bytes().all(|b| b.is_ascii_hexdigit()))
    {
        return Err("Incomplete server capabilities or asset identity".into());
    }
    if id.mode != "development" && (id.mode != "release" || id.dirty) {
        return Err("Invalid build identity".into());
    }
    Ok(())
}

/// Managed downloads must match the pinned, clean release, while user-provided
/// serverPath/PATH builds retain the development-friendly contract above.
pub fn validate_release(bytes: &[u8], version: &str) -> Result<(), String> {
    validate(bytes)?;
    let value: serde_json::Value = serde_json::from_slice(bytes).map_err(|e| e.to_string())?;
    let commit = value["sourceCommit"].as_str().unwrap_or("");
    if value["serverVersion"] != version
        || value["buildMode"] != "release"
        || value["dirty"] != false
        || commit.len() != 40
        || !commit
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
    {
        return Err(format!(
            "Downloaded server must be a clean release matching version {version}"
        ));
    }
    Ok(())
}
