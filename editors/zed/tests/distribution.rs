//! Acceptance tests use the production installer and real temporary files.
//! Zed's network, chmod and process boundary is substituted. Publication hooks
//! interleave another installer immediately before real filesystem renames.
//! Native process execution remains covered by the existing package consumers.
#[path = "../src/distribution.rs"]
mod distribution;
#[path = "../src/identity.rs"]
mod identity;

use distribution::{Host, Target};
use serde_json::json;
use sha2::{Digest, Sha256};
use std::{
    cell::RefCell,
    collections::HashMap,
    fs,
    path::{Path, PathBuf},
    rc::Rc,
    sync::atomic::{AtomicU64, Ordering},
    time::{SystemTime, UNIX_EPOCH},
};

const VERSION: &str = "0.1.0";
const BASE: &str = "https://github.com/lazysnail1024/zed-argocd/releases/download";
static NEXT: AtomicU64 = AtomicU64::new(0);

struct Scratch(PathBuf);
impl Scratch {
    fn new() -> Self {
        let path = std::env::temp_dir().join(format!(
            "argocd-install-{}-{}-{}",
            std::process::id(),
            SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap()
                .as_nanos(),
            NEXT.fetch_add(1, Ordering::Relaxed)
        ));
        fs::create_dir(&path).unwrap();
        Self(path)
    }
    fn slot(&self, target: Target, version: &str) -> PathBuf {
        self.0
            .join("servers")
            .join(format!("v{version}-{}", target.platform))
    }
    fn generations(&self, target: Target, version: &str) -> Vec<PathBuf> {
        let prefix = format!("v{version}-{}.gen-", target.platform);
        fs::read_dir(self.0.join("servers"))
            .unwrap()
            .map(|e| e.unwrap().path())
            .filter(|p| {
                p.file_name()
                    .unwrap()
                    .to_string_lossy()
                    .starts_with(&prefix)
            })
            .collect()
    }
    fn staging(&self) -> Vec<PathBuf> {
        fs::read_dir(self.0.join("servers"))
            .unwrap()
            .map(|e| e.unwrap().path())
            .filter(|p| {
                p.file_name()
                    .unwrap()
                    .to_string_lossy()
                    .starts_with(".stage-")
            })
            .collect()
    }
}
impl Drop for Scratch {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

fn target() -> Target {
    Target::resolve("linux", "x86_64").unwrap()
}
fn digest(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}
fn release(version: &str) -> Vec<u8> {
    serde_json::to_vec(&json!({
        "product": "argo-cd", "serverVersion": version,
        "adapterContractVersion": 1, "analysisReady": true,
        "configSchemaVersions": [1], "reportSchemaVersions": [1],
        "toolkitVersion": "fixture", "catalogHash": "a".repeat(64),
        "sourceCommit": "b".repeat(40), "dirty": false, "buildMode": "release"
    }))
    .unwrap()
}
fn sums(target: Target, version: &str, bytes: &[u8]) -> Vec<u8> {
    format!("{}  {}\n", digest(bytes), target.asset(version)).into_bytes()
}

#[derive(Default)]
struct FakeHost {
    assets: HashMap<String, Vec<u8>>,
    downloads: Vec<String>,
    permissions: Vec<PathBuf>,
    probes: Vec<PathBuf>,
    interrupted: Option<String>,
    download_error: Option<(String, String)>,
    permission_error: bool,
    probe_error: bool,
    winner: Option<PathBuf>,
    vanished_staging: bool,
    staged_directory: Option<PathBuf>,
    publisher: Option<Box<dyn FnMut(&Path, &Path) -> std::io::Result<()>>>,
    #[cfg(unix)]
    replace_stage_with_symlink: Option<PathBuf>,
    #[cfg(unix)]
    replace_binary_with_symlink: Option<PathBuf>,
}
impl FakeHost {
    fn online(target: Target, version: &str) -> Self {
        let mut host = Self::default();
        let bytes = release(version);
        host.assets.insert(
            format!("{BASE}/v{version}/SHA256SUMS"),
            sums(target, version, &bytes),
        );
        host.assets.insert(
            format!("{BASE}/v{version}/{}", target.asset(version)),
            bytes,
        );
        host
    }
    fn binary_mut(&mut self, target: Target, version: &str) -> &mut Vec<u8> {
        self.assets
            .get_mut(&format!("{BASE}/v{version}/{}", target.asset(version)))
            .unwrap()
    }
    fn manifest_mut(&mut self, version: &str) -> &mut Vec<u8> {
        self.assets
            .get_mut(&format!("{BASE}/v{version}/SHA256SUMS"))
            .unwrap()
    }
    fn changed_identity(&mut self, field: &str, value: serde_json::Value) {
        let binary = self.binary_mut(target(), VERSION);
        let mut id: serde_json::Value = serde_json::from_slice(binary).unwrap();
        id[field] = value;
        *binary = serde_json::to_vec(&id).unwrap();
        let manifest = sums(target(), VERSION, binary);
        *self.manifest_mut(VERSION) = manifest;
    }
}
impl Host for FakeHost {
    fn publish_directory(&mut self, staging: &Path, destination: &Path) -> std::io::Result<()> {
        match &mut self.publisher {
            Some(publisher) => publisher(staging, destination),
            None => fs::rename(staging, destination),
        }
    }
    fn download(&mut self, url: &str, destination: &Path) -> Result<(), String> {
        self.downloads.push(url.to_owned());
        if let Some((failed_url, message)) = &self.download_error {
            if url == failed_url {
                return Err(message.clone());
            }
        }
        let bytes = self
            .assets
            .get(url)
            .ok_or("offline, 404, or download denied")?;
        if self
            .interrupted
            .as_deref()
            .is_some_and(|suffix| url.ends_with(suffix))
        {
            fs::write(destination, &bytes[..bytes.len() / 2]).unwrap();
            return Err("download interrupted after partial write".into());
        }
        fs::write(destination, bytes).map_err(|e| e.to_string())?;
        #[cfg(unix)]
        if url.ends_with("SHA256SUMS") {
            if let Some(external) = self.replace_stage_with_symlink.take() {
                let dir = destination.parent().unwrap();
                fs::remove_dir_all(dir).unwrap();
                std::os::unix::fs::symlink(external, dir).unwrap();
            }
        } else if let Some(external) = self.replace_binary_with_symlink.take() {
            fs::remove_file(destination).unwrap();
            std::os::unix::fs::symlink(external, destination).unwrap();
        }
        Ok(())
    }
    fn executable(&mut self, path: &Path) -> Result<(), String> {
        self.permissions.push(path.to_owned());
        if self.permission_error {
            Err("make_file_executable denied".into())
        } else {
            Ok(())
        }
    }
    fn identity(&mut self, path: &Path, version: &str) -> Result<(), String> {
        self.probes.push(path.to_owned());
        if self.probe_error {
            return Err("identity process did not complete successfully".into());
        }
        identity::validate_release(&fs::read(path).unwrap(), version)?;
        if path
            .parent()
            .unwrap()
            .file_name()
            .unwrap()
            .to_string_lossy()
            .starts_with(".stage-")
        {
            self.staged_directory = Some(path.parent().unwrap().to_owned());
            if let Some(winner) = self.winner.take() {
                fs::create_dir_all(&winner).unwrap();
                fs::copy(path, winner.join(path.file_name().unwrap())).unwrap();
                fs::copy(
                    path.parent().unwrap().join("SHA256SUMS"),
                    winner.join("SHA256SUMS"),
                )
                .unwrap();
                fs::write(winner.join("winner-marker"), b"another Zed process").unwrap();
            }
        } else if self.vanished_staging {
            if let Some(stage) = self.staged_directory.take() {
                fs::remove_dir_all(stage).unwrap();
            }
        }
        Ok(())
    }
}

#[test]
fn all_five_platforms_install_the_exact_asset_and_reuse_cache_offline() {
    for (os, arch, platform, exe) in [
        ("linux", "x86_64", "linux-amd64", "argocd-ls"),
        ("linux", "aarch64", "linux-arm64", "argocd-ls"),
        ("macos", "x86_64", "darwin-amd64", "argocd-ls"),
        ("macos", "aarch64", "darwin-arm64", "argocd-ls"),
        ("windows", "x86_64", "windows-amd64", "argocd-ls.exe"),
    ] {
        let target = Target::resolve(os, arch).unwrap();
        assert_eq!((target.platform, target.executable), (platform, exe));
        let root = Scratch::new();
        let mut host = FakeHost::online(target, VERSION);
        let installed = distribution::install(&mut host, &root.0, target, VERSION).unwrap();
        assert!(root
            .generations(target, VERSION)
            .contains(&installed.parent().unwrap().to_owned()));
        assert_eq!(installed.file_name().unwrap(), exe);
        assert!(!root.slot(target, VERSION).exists());
        assert!(installed.is_absolute());
        assert_eq!(fs::read(&installed).unwrap(), release(VERSION));
        assert_eq!(
            host.downloads,
            [
                format!("{BASE}/v{VERSION}/SHA256SUMS"),
                format!("{BASE}/v{VERSION}/{}", target.asset(VERSION))
            ]
        );
        assert_eq!(host.probes.len(), 1);
        assert!(root.staging().is_empty());
        // A newly-created host instance has no network fixtures. Every startup
        // still rechecks the checksum and process identity of the disk cache.
        let mut offline = FakeHost::default();
        assert_eq!(
            distribution::install(&mut offline, &root.0, target, VERSION).unwrap(),
            installed
        );
        assert!(offline.downloads.is_empty());
        assert_eq!(offline.probes, [installed]);
    }
    assert!(Target::resolve("windows", "aarch64").is_err());
    assert!(Target::resolve("linux", "riscv64").is_err());
}

#[test]
fn bad_checksums_never_make_the_binary_executable_or_probe_it() {
    let good = sums(target(), VERSION, &release(VERSION));
    let wrong_asset = String::from_utf8(good.clone())
        .unwrap()
        .replace("linux-amd64", "linux-arm64")
        .into_bytes();
    for (name, manifest) in [
        ("empty", vec![]),
        ("malformed", b"not-a-checksum\n".to_vec()),
        ("non-utf8", vec![255]),
        ("wrong-asset", wrong_asset),
        ("duplicate", [good.clone(), good.clone()].concat()),
        ("truncated", good[..50].to_vec()),
        (
            "wrong-sha",
            format!("{}  {}\n", "0".repeat(64), target().asset(VERSION)).into_bytes(),
        ),
        (
            "traversal",
            format!("{}  ../{}\n", "0".repeat(64), target().asset(VERSION)).into_bytes(),
        ),
        ("oversized", vec![b'a'; 64 * 1024 + 1]),
    ] {
        let root = Scratch::new();
        let mut host = FakeHost::online(target(), VERSION);
        *host.manifest_mut(VERSION) = manifest;
        let result = distribution::install(&mut host, &root.0, target(), VERSION);
        assert!(result.is_err(), "{name} was accepted");
        assert!(host.permissions.is_empty(), "{name} reached chmod");
        assert!(host.probes.is_empty(), "{name} reached process execution");
        assert!(!root.slot(target(), VERSION).exists());
        assert!(root.generations(target(), VERSION).is_empty());
        assert!(root.staging().is_empty());
        if name != "wrong-sha" {
            assert_eq!(host.downloads.len(), 1, "{name}");
        }
    }
}

#[test]
fn empty_truncated_and_corrupted_binary_downloads_are_rejected_before_execution() {
    for bytes in [
        vec![],
        b"truncated".to_vec(),
        vec![b'x'; release(VERSION).len()],
    ] {
        let root = Scratch::new();
        let mut host = FakeHost::online(target(), VERSION);
        *host.binary_mut(target(), VERSION) = bytes;
        assert!(distribution::install(&mut host, &root.0, target(), VERSION).is_err());
        assert!(host.permissions.is_empty());
        assert!(host.probes.is_empty());
        assert!(root.staging().is_empty());
    }
}

#[test]
fn download_interruption_cleans_staging_and_retry_completes() {
    for interrupted in ["SHA256SUMS".to_owned(), target().asset(VERSION)] {
        let root = Scratch::new();
        let mut host = FakeHost::online(target(), VERSION);
        host.interrupted = Some(interrupted.clone());
        let error = distribution::install(&mut host, &root.0, target(), VERSION).unwrap_err();
        assert!(error.contains("interrupted"));
        assert!(error.contains(&format!("{BASE}/v{VERSION}/{interrupted}")));
        assert!(error.contains("settings.serverPath"));
        assert!(root.staging().is_empty());
        assert!(host.probes.is_empty());
        host.interrupted = None;
        assert!(distribution::install(&mut host, &root.0, target(), VERSION)
            .unwrap()
            .is_file());
    }
}

#[test]
fn download_failures_identify_the_exact_stage_and_preserve_offline_versions() {
    for message in [
        "HTTP 403 Forbidden",
        "HTTP 404 Not Found",
        "request timed out",
    ] {
        for (asset, phase) in [
            ("SHA256SUMS".to_owned(), "checksum manifest"),
            (target().asset("0.2.0"), "native server"),
        ] {
            let root = Scratch::new();
            let mut old = FakeHost::online(target(), VERSION);
            let old_path = distribution::install(&mut old, &root.0, target(), VERSION).unwrap();
            let old_bytes = fs::read(&old_path).unwrap();
            let url = format!("{BASE}/v0.2.0/{asset}");
            let mut next = FakeHost::online(target(), "0.2.0");
            next.download_error = Some((url.clone(), message.to_owned()));
            let error = distribution::install(&mut next, &root.0, target(), "0.2.0").unwrap_err();
            for detail in [
                phase,
                &url,
                message,
                "v0.2.0",
                "linux-amd64",
                "settings.serverPath",
            ] {
                assert!(error.contains(detail), "missing {detail:?}: {error}");
            }
            assert_eq!(next.downloads.last(), Some(&url));
            assert!(next.permissions.is_empty());
            assert!(next.probes.is_empty());
            assert!(root.staging().is_empty());
            assert!(root.generations(target(), "0.2.0").is_empty());
            assert_eq!(fs::read(&old_path).unwrap(), old_bytes);
            let mut offline = FakeHost::default();
            assert_eq!(
                distribution::install(&mut offline, &root.0, target(), VERSION).unwrap(),
                old_path
            );
            assert!(offline.downloads.is_empty());
            next.download_error = None;
            let new_path = distribution::install(&mut next, &root.0, target(), "0.2.0").unwrap();
            assert_eq!(fs::read(new_path).unwrap(), release("0.2.0"));
            assert_eq!(fs::read(old_path).unwrap(), old_bytes);
        }
    }
}

#[test]
fn missing_release_or_denied_download_is_actionable_and_does_not_leave_an_install() {
    let root = Scratch::new();
    let mut host = FakeHost::default();
    let error = distribution::install(&mut host, &root.0, target(), VERSION).unwrap_err();
    assert!(
        error.contains("v0.1.0")
            && error.contains("linux-amd64")
            && error.contains("settings.serverPath")
    );
    assert!(host.probes.is_empty());
    assert!(!root.slot(target(), VERSION).exists());
    assert!(root.generations(target(), VERSION).is_empty());
    assert!(root.staging().is_empty());
}

#[test]
fn chmod_or_process_failure_never_activates_staging_and_can_be_retried() {
    for permission_error in [true, false] {
        let root = Scratch::new();
        let mut host = FakeHost::online(target(), VERSION);
        host.permission_error = permission_error;
        host.probe_error = !permission_error;
        assert!(distribution::install(&mut host, &root.0, target(), VERSION).is_err());
        assert!(!root.slot(target(), VERSION).exists());
        assert!(root.generations(target(), VERSION).is_empty());
        assert!(root.staging().is_empty());
        assert_eq!(host.probes.is_empty(), permission_error);
        host.permission_error = false;
        host.probe_error = false;
        assert!(distribution::install(&mut host, &root.0, target(), VERSION).is_ok());
    }
}

#[test]
fn managed_identity_is_exact_while_custom_development_identity_remains_supported() {
    for (field, value) in [
        ("product", json!("argo-workflows")),
        ("serverVersion", json!("0.2.0")),
        ("adapterContractVersion", json!(2)),
        ("analysisReady", json!(false)),
        ("buildMode", json!("development")),
        ("dirty", json!(true)),
        ("sourceCommit", json!("unknown")),
    ] {
        let root = Scratch::new();
        let mut host = FakeHost::online(target(), VERSION);
        host.changed_identity(field, value);
        assert!(
            distribution::install(&mut host, &root.0, target(), VERSION).is_err(),
            "{field}"
        );
        assert_eq!(host.probes.len(), 1);
        assert!(!root.slot(target(), VERSION).exists());
        assert!(root.generations(target(), VERSION).is_empty());
        assert!(root.staging().is_empty());
    }
    let mut development: serde_json::Value = serde_json::from_slice(&release(VERSION)).unwrap();
    development["buildMode"] = json!("development");
    development["dirty"] = json!(true);
    development["sourceCommit"] = json!(null);
    let bytes = serde_json::to_vec(&development).unwrap();
    assert!(identity::validate(&bytes).is_ok());
    assert!(identity::validate_release(&bytes, VERSION).is_err());
}

#[test]
fn corrupt_or_missing_cache_is_not_executed_and_is_repaired_on_retry() {
    for fault in ["binary", "checksum", "missing-binary", "missing-checksum"] {
        let root = Scratch::new();
        let mut online = FakeHost::online(target(), VERSION);
        let path = distribution::install(&mut online, &root.0, target(), VERSION).unwrap();
        let sum = path.parent().unwrap().join("SHA256SUMS");
        match fault {
            "binary" => fs::write(&path, b"corrupted cache").unwrap(),
            "checksum" => fs::write(&sum, b"invalid checksum").unwrap(),
            "missing-binary" => fs::remove_file(&path).unwrap(),
            _ => fs::remove_file(&sum).unwrap(),
        }
        let mut offline = FakeHost::default();
        assert!(
            distribution::install(&mut offline, &root.0, target(), VERSION).is_err(),
            "{fault}"
        );
        assert!(offline.probes.is_empty(), "{fault} cache was executed");
        let old_binary = fs::read(&path).ok();
        let old_sums = fs::read(&sum).ok();
        let mut repair = FakeHost::online(target(), VERSION);
        let repaired = distribution::install(&mut repair, &root.0, target(), VERSION).unwrap();
        assert_ne!(repaired, path);
        assert_eq!(fs::read(&repaired).unwrap(), release(VERSION));
        assert_eq!(fs::read(&path).ok(), old_binary);
        assert_eq!(fs::read(&sum).ok(), old_sums);
        let mut offline_repaired = FakeHost::default();
        assert_eq!(
            distribution::install(&mut offline_repaired, &root.0, target(), VERSION).unwrap(),
            repaired
        );
        assert!(offline_repaired.downloads.is_empty());
        assert_eq!(repair.downloads.len(), 2);
        assert_eq!(repair.probes.len(), 1);
        assert!(root.staging().is_empty());
    }
}

#[test]
fn mismatched_cached_identity_cannot_satisfy_a_pinned_install() {
    let root = Scratch::new();
    let mut online = FakeHost::online(target(), VERSION);
    let path = distribution::install(&mut online, &root.0, target(), VERSION).unwrap();
    let other = release("0.2.0");
    fs::write(&path, &other).unwrap();
    fs::write(
        path.parent().unwrap().join("SHA256SUMS"),
        sums(target(), VERSION, &other),
    )
    .unwrap();
    let mut offline = FakeHost::default();
    let error = distribution::install(&mut offline, &root.0, target(), VERSION).unwrap_err();
    assert!(error.contains("matching version 0.1.0"));
    assert_eq!(offline.probes, [path]);
}

#[test]
fn upgrade_failure_keeps_old_version_but_never_silently_falls_back() {
    let root = Scratch::new();
    let mut old = FakeHost::online(target(), VERSION);
    let old_path = distribution::install(&mut old, &root.0, target(), VERSION).unwrap();
    let old_bytes = fs::read(&old_path).unwrap();
    let mut unavailable = FakeHost::default();
    assert!(distribution::install(&mut unavailable, &root.0, target(), "0.2.0").is_err());
    assert!(unavailable.probes.is_empty());
    assert!(!root.slot(target(), "0.2.0").exists());
    assert_eq!(fs::read(&old_path).unwrap(), old_bytes);
    let mut next = FakeHost::online(target(), "0.2.0");
    let next_path = distribution::install(&mut next, &root.0, target(), "0.2.0").unwrap();
    assert_ne!(old_path, next_path);
    assert_eq!(fs::read(&old_path).unwrap(), old_bytes);
    assert_eq!(fs::read(next_path).unwrap(), release("0.2.0"));
    let mut rollback = FakeHost::default();
    assert_eq!(
        distribution::install(&mut rollback, &root.0, target(), VERSION).unwrap(),
        old_path
    );
    assert!(rollback.downloads.is_empty());
}

#[test]
fn an_existing_verified_concurrent_winner_is_reused_without_replacing_it() {
    for vanished_staging in [false, true] {
        let root = Scratch::new();
        let mut host = FakeHost::online(target(), VERSION);
        host.winner = Some(root.slot(target(), VERSION));
        host.vanished_staging = vanished_staging;
        let path = distribution::install(&mut host, &root.0, target(), VERSION).unwrap();
        assert!(path.parent().unwrap().join("winner-marker").is_file());
        assert_eq!(host.probes.len(), 2);
        assert!(root.staging().is_empty());
    }
}

#[test]
fn failed_generation_publication_preserves_invalid_legacy_cache_and_retry_can_repair() {
    let root = Scratch::new();
    let slot = root.slot(target(), VERSION);
    fs::create_dir_all(&slot).unwrap();
    fs::write(slot.join("preserved"), b"old bytes").unwrap();
    let mut host = FakeHost::online(target(), VERSION);
    host.publisher = Some(Box::new(|_, _| {
        Err(std::io::Error::new(
            std::io::ErrorKind::PermissionDenied,
            "publication denied",
        ))
    }));
    assert!(distribution::install(&mut host, &root.0, target(), VERSION)
        .unwrap_err()
        .contains("Cannot publish verified server generation"));
    assert_eq!(fs::read(slot.join("preserved")).unwrap(), b"old bytes");
    assert!(root.staging().is_empty());
    assert!(root.generations(target(), VERSION).is_empty());
    // Retrying publishes a sibling and never moves or deletes the legacy slot.
    let mut retry = FakeHost::online(target(), VERSION);
    let repaired = distribution::install(&mut retry, &root.0, target(), VERSION).unwrap();
    assert_ne!(repaired.parent().unwrap(), slot);
    assert_eq!(fs::read(slot.join("preserved")).unwrap(), b"old bytes");
}

#[test]
fn abandoned_staging_does_not_block_a_new_attempt_or_get_selected() {
    let root = Scratch::new();
    let stale = root.0.join("servers/.stage-abandoned");
    fs::create_dir_all(&stale).unwrap();
    fs::write(stale.join("argocd-ls"), b"partial download").unwrap();
    let mut host = FakeHost::online(target(), VERSION);
    let path = distribution::install(&mut host, &root.0, target(), VERSION).unwrap();
    assert!(root
        .generations(target(), VERSION)
        .contains(&path.parent().unwrap().to_owned()));
    assert_eq!(
        fs::read(stale.join("argocd-ls")).unwrap(),
        b"partial download"
    );
}

#[test]
fn explicit_path_wins_and_invalid_configuration_does_not_fall_back() {
    let configured = json!("/opt/argo custom/argocd-ls");
    assert_eq!(
        distribution::local_path(Some(&configured), Some("/usr/bin/argocd-ls".into()), false)
            .unwrap()
            .as_deref(),
        Some("/opt/argo custom/argocd-ls")
    );
    for invalid in [
        json!(null),
        json!(false),
        json!(""),
        json!("relative/argocd-ls"),
        json!("C:relative.exe"),
    ] {
        assert!(
            distribution::local_path(Some(&invalid), Some("/valid/fallback".into()), false)
                .is_err()
        );
    }
    assert_eq!(
        distribution::local_path(None, Some("/usr/bin/argocd-ls".into()), false)
            .unwrap()
            .as_deref(),
        Some("/usr/bin/argocd-ls")
    );
    assert_eq!(distribution::local_path(None, None, false).unwrap(), None);
    for path in [
        "C:/tools/argocd-ls.exe",
        "D:\\tools\\argocd-ls.exe",
        "\\\\host\\share\\argocd-ls.exe",
        "//host/share/argocd-ls.exe",
    ] {
        assert_eq!(
            distribution::local_path(Some(&json!(path)), None, true)
                .unwrap()
                .as_deref(),
            Some(path)
        );
    }
    for path in [
        "C:argocd-ls.exe",
        "/unix/path",
        "relative.exe",
        "1:/file.exe",
    ] {
        assert!(
            distribution::local_path(Some(&json!(path)), None, true).is_err(),
            "{path}"
        );
    }
}

#[test]
fn invalid_pins_cannot_escape_or_select_arbitrary_release_urls() {
    for pin in [
        "../0.1.0",
        "0.1.0/other",
        "latest",
        "v0.1.0",
        "0.1.0-alpha..1",
        "0.1.0-alpha.01",
        "0.1.0-",
        "00.1.0",
        "0.1.0+build.1",
        "0.1.0-alpha/1",
        "0.1.0\n",
        "0.1",
    ] {
        let root = Scratch::new();
        let mut host = FakeHost::default();
        assert!(distribution::install(&mut host, &root.0, target(), pin).is_err());
        assert!(host.downloads.is_empty());
        assert!(!root.0.join("servers").exists());
    }
}

#[test]
fn pinned_alpha_is_verified_cached_and_rejects_another_release_identity() {
    let version = distribution::server_version();
    assert_eq!(version, "0.0.1-alpha.2");
    let root = Scratch::new();
    let mut host = FakeHost::online(target(), version);
    let path = distribution::install(&mut host, &root.0, target(), version).unwrap();
    assert_eq!(
        host.downloads,
        [
            format!("{BASE}/v0.0.1-alpha.2/SHA256SUMS"),
            format!("{BASE}/v0.0.1-alpha.2/argocd-ls-0.0.1-alpha.2-linux-amd64")
        ]
    );
    assert_eq!(fs::read(&path).unwrap(), release(version));
    let mut offline = FakeHost::default();
    assert_eq!(
        distribution::install(&mut offline, &root.0, target(), version).unwrap(),
        path
    );
    assert!(offline.downloads.is_empty());

    let root = Scratch::new();
    let mut wrong = FakeHost::online(target(), version);
    *wrong.binary_mut(target(), version) = release("0.0.1-alpha.1");
    *wrong.manifest_mut(version) = sums(target(), version, &release("0.0.1-alpha.1"));
    let error = distribution::install(&mut wrong, &root.0, target(), version).unwrap_err();
    assert!(error.contains("matching version 0.0.1-alpha.2"));
    assert!(root.generations(target(), version).is_empty());
}

#[test]
fn stable_and_prerelease_pins_use_the_exact_release() {
    for version in ["0.0.1", "0.0.1-alpha.1", "1.2.3-rc.0", "2.0.0-beta-test.2"] {
        let root = Scratch::new();
        let mut host = FakeHost::online(target(), version);
        let path = distribution::install(&mut host, &root.0, target(), version).unwrap();
        assert_eq!(fs::read(path).unwrap(), release(version));
        assert!(host.downloads[0].ends_with(&format!("/v{version}/SHA256SUMS")));
    }
}

#[cfg(unix)]
#[test]
fn cache_and_download_symlinks_are_refused_without_touching_external_data() {
    for location in [
        "servers",
        "slot",
        "checksum",
        "binary",
        "stage",
        "download-binary",
    ] {
        let root = Scratch::new();
        let external = Scratch::new();
        let sentinel = external.0.join("sentinel");
        fs::write(&sentinel, b"untouched").unwrap();
        fs::write(
            external.0.join("SHA256SUMS"),
            sums(target(), VERSION, &release(VERSION)),
        )
        .unwrap();
        let external_binary = external.0.join("argocd-ls");
        fs::write(&external_binary, release(VERSION)).unwrap();
        let slot = root.slot(target(), VERSION);
        let mut host = FakeHost::online(target(), VERSION);
        match location {
            "servers" => std::os::unix::fs::symlink(&external.0, root.0.join("servers")).unwrap(),
            "slot" => {
                fs::create_dir(root.0.join("servers")).unwrap();
                std::os::unix::fs::symlink(&external.0, &slot).unwrap();
            }
            "stage" => host.replace_stage_with_symlink = Some(external.0.clone()),
            "download-binary" => host.replace_binary_with_symlink = Some(external_binary.clone()),
            _ => {
                fs::create_dir_all(&slot).unwrap();
                fs::write(
                    slot.join("SHA256SUMS"),
                    sums(target(), VERSION, &release(VERSION)),
                )
                .unwrap();
                fs::write(slot.join("argocd-ls"), release(VERSION)).unwrap();
                let (link, destination) = if location == "checksum" {
                    (slot.join("SHA256SUMS"), external.0.join("SHA256SUMS"))
                } else {
                    (slot.join("argocd-ls"), external_binary.clone())
                };
                fs::remove_file(&link).unwrap();
                std::os::unix::fs::symlink(destination, link).unwrap();
                host.assets.clear();
            }
        }
        assert!(
            distribution::install(&mut host, &root.0, target(), VERSION).is_err(),
            "{location}"
        );
        assert!(
            host.probes.is_empty(),
            "{location} reached process execution"
        );
        assert_eq!(fs::read(&sentinel).unwrap(), b"untouched");
        assert_eq!(fs::read(&external_binary).unwrap(), release(VERSION));
    }
}

fn seed_legacy(root: &Scratch, target: Target, version: &str) -> PathBuf {
    let slot = root.slot(target, version);
    fs::create_dir_all(&slot).unwrap();
    fs::write(
        slot.join("SHA256SUMS"),
        sums(target, version, &release(version)),
    )
    .unwrap();
    let binary = slot.join(target.executable);
    fs::write(&binary, release(version)).unwrap();
    binary
}

#[test]
fn a_valid_legacy_slot_still_requests_permission_and_identity_checks_offline() {
    let root = Scratch::new();
    let legacy = seed_legacy(&root, target(), VERSION);
    let sentinel = legacy.parent().unwrap().join("legacy-owner");
    fs::write(&sentinel, b"preserved legacy path").unwrap();
    let mut offline = FakeHost::default();
    assert_eq!(
        distribution::install(&mut offline, &root.0, target(), VERSION).unwrap(),
        legacy
    );
    assert!(offline.downloads.is_empty());
    assert_eq!(offline.permissions, [legacy.clone()]);
    assert_eq!(offline.probes, [legacy]);
    assert_eq!(fs::read(sentinel).unwrap(), b"preserved legacy path");
    assert!(root.generations(target(), VERSION).is_empty());
}

#[test]
fn a_competitor_publishing_after_the_last_cache_check_keeps_its_returned_path() {
    for damaged in ["fresh", "legacy", "generation"] {
        let root = Scratch::new();
        let damaged_path = match damaged {
            "legacy" => Some(seed_legacy(&root, target(), VERSION)),
            "generation" => Some(
                distribution::install(
                    &mut FakeHost::online(target(), VERSION),
                    &root.0,
                    target(),
                    VERSION,
                )
                .unwrap(),
            ),
            _ => None,
        };
        if let Some(path) = &damaged_path {
            fs::write(path, b"corrupt published bytes").unwrap();
            fs::write(path.parent().unwrap().join("old-owner"), b"do not remove").unwrap();
        }
        let winner_path = Rc::new(RefCell::new(None::<PathBuf>));
        let recorded = Rc::clone(&winner_path);
        let shared_root = root.0.clone();
        let mut interleaved = false;
        let mut first = FakeHost::online(target(), VERSION);
        first.publisher = Some(Box::new(move |staging, destination| {
            // This hook is after A's last cache lookup and target-existence
            // check. Run B all the way through install() and return, then put a
            // sentinel at B's returned location before A's actual rename.
            if !interleaved {
                interleaved = true;
                let mut second = FakeHost::online(target(), VERSION);
                let winner =
                    distribution::install(&mut second, &shared_root, target(), VERSION).unwrap();
                fs::write(
                    winner.parent().unwrap().join("winner-returned"),
                    b"B already returned this path",
                )
                .unwrap();
                *recorded.borrow_mut() = Some(winner);
            }
            fs::rename(staging, destination)
        }));
        let first_path = distribution::install(&mut first, &root.0, target(), VERSION).unwrap();
        let second_path = winner_path.borrow().clone().unwrap();
        assert_eq!(
            fs::read(&first_path).unwrap(),
            release(VERSION),
            "{damaged}: A path"
        );
        assert_eq!(
            fs::read(&second_path).unwrap(),
            release(VERSION),
            "{damaged}: B path"
        );
        assert_eq!(
            fs::read(second_path.parent().unwrap().join("winner-returned")).unwrap(),
            b"B already returned this path",
            "{damaged}"
        );
        if let Some(path) = damaged_path {
            assert_eq!(fs::read(&path).unwrap(), b"corrupt published bytes");
            assert_eq!(
                fs::read(path.parent().unwrap().join("old-owner")).unwrap(),
                b"do not remove"
            );
        }
        let mut offline = FakeHost::default();
        let warm = distribution::install(&mut offline, &root.0, target(), VERSION).unwrap();
        assert!(warm == first_path || warm == second_path);
        assert_eq!(fs::read(warm).unwrap(), release(VERSION));
        assert!(offline.downloads.is_empty());
        assert!(root.staging().is_empty());
    }
}

#[test]
fn an_exact_destination_collision_revalidates_the_nonempty_winner_for_all_error_kinds() {
    for permission_denied in [false, true] {
        let root = Scratch::new();
        let returned = Rc::new(RefCell::new(None::<PathBuf>));
        let recorded = Rc::clone(&returned);
        let mut host = FakeHost::online(target(), VERSION);
        host.publisher = Some(Box::new(move |staging, destination| {
            // Force a collision that timestamp uniqueness normally avoids. A
            // second complete directory is really renamed into A's exact target
            // after A observed it absent; no empty published reservation exists.
            let second_stage = staging.with_extension("second");
            fs::create_dir(&second_stage).unwrap();
            for file in ["SHA256SUMS", "argocd-ls"] {
                fs::copy(staging.join(file), second_stage.join(file)).unwrap();
            }
            fs::rename(&second_stage, destination).unwrap();
            let winner = destination.join("argocd-ls");
            fs::write(destination.join("winner-returned"), b"collision winner").unwrap();
            *recorded.borrow_mut() = Some(winner);
            let actual = fs::rename(staging, destination).unwrap_err();
            if permission_denied {
                // Windows can use PermissionDenied for a nonempty destination.
                Err(std::io::Error::new(
                    std::io::ErrorKind::PermissionDenied,
                    actual,
                ))
            } else {
                Err(actual)
            }
        }));
        let selected = distribution::install(&mut host, &root.0, target(), VERSION).unwrap();
        assert_eq!(Some(selected.clone()), *returned.borrow());
        assert_eq!(fs::read(&selected).unwrap(), release(VERSION));
        assert_eq!(
            fs::read(selected.parent().unwrap().join("winner-returned")).unwrap(),
            b"collision winner"
        );
        assert_eq!(host.probes.len(), 2); // staged bytes, then the exact winner
        assert!(root.staging().is_empty());
        let mut offline = FakeHost::default();
        assert_eq!(
            distribution::install(&mut offline, &root.0, target(), VERSION).unwrap(),
            selected
        );
        assert!(offline.downloads.is_empty());
    }
}

#[test]
fn an_invalid_nonempty_collision_is_preserved_while_a_fresh_generation_is_published() {
    let root = Scratch::new();
    let collided = Rc::new(RefCell::new(None::<PathBuf>));
    let recorded = Rc::clone(&collided);
    let mut first = true;
    let mut host = FakeHost::online(target(), VERSION);
    host.publisher = Some(Box::new(move |staging, destination| {
        if first {
            first = false;
            fs::create_dir(destination).unwrap();
            fs::write(
                destination.join("unrelated-owner"),
                b"do not delete invalid collision",
            )
            .unwrap();
            *recorded.borrow_mut() = Some(destination.to_owned());
        }
        fs::rename(staging, destination)
    }));
    let selected = distribution::install(&mut host, &root.0, target(), VERSION).unwrap();
    let collided = collided.borrow().clone().unwrap();
    assert_ne!(selected.parent().unwrap(), collided);
    assert_eq!(
        fs::read(collided.join("unrelated-owner")).unwrap(),
        b"do not delete invalid collision"
    );
    assert_eq!(fs::read(selected).unwrap(), release(VERSION));
    assert!(root.staging().is_empty());
}

#[test]
fn a_successful_rename_relinquishes_the_former_staging_path_before_cleanup() {
    let root = Scratch::new();
    let reused = Rc::new(RefCell::new(None::<PathBuf>));
    let recorded = Rc::clone(&reused);
    let mut host = FakeHost::online(target(), VERSION);
    host.publisher = Some(Box::new(move |staging, destination| {
        fs::rename(staging, destination)?;
        // B reserves the now-unused staging pathname before A resumes. Its
        // partial data must not be removed by A's successful-install cleanup.
        fs::create_dir(staging).unwrap();
        fs::write(
            staging.join("next-installer"),
            b"B owns this staging directory",
        )
        .unwrap();
        *recorded.borrow_mut() = Some(staging.to_owned());
        Ok(())
    }));
    let selected = distribution::install(&mut host, &root.0, target(), VERSION).unwrap();
    let reused = reused.borrow().clone().unwrap();
    assert_eq!(
        fs::read(reused.join("next-installer")).unwrap(),
        b"B owns this staging directory"
    );
    assert_eq!(fs::read(&selected).unwrap(), release(VERSION));
    let mut offline = FakeHost::default();
    assert_eq!(
        distribution::install(&mut offline, &root.0, target(), VERSION).unwrap(),
        selected
    );
    assert!(offline.downloads.is_empty());
    assert!(reused.join("next-installer").is_file());
}

#[test]
fn cache_enumeration_only_accepts_exact_version_platform_and_complete_generation_names() {
    let root = Scratch::new();
    fs::create_dir(root.0.join("servers")).unwrap();
    for name in [
        "v0.2.0-linux-amd64.gen-1-0",
        "v0.1.0-linux-arm64.gen-1-0",
        "v0.1.0-linux-amd64.gen-1",
        "v0.1.0-linux-amd64.gen--0",
        "v0.1.0-linux-amd64.gen-1-0-extra",
        "v0.1.0-linux-amd64.gen-+1-0",
        ".stage-v0.1.0-linux-amd64.gen-1-0",
        "v0.1.0-linux-amd64.gen-9999999999999999999999999999999999999999-0",
    ] {
        let candidate = root.0.join("servers").join(name);
        fs::create_dir(&candidate).unwrap();
        fs::write(
            candidate.join("SHA256SUMS"),
            sums(target(), VERSION, &release(VERSION)),
        )
        .unwrap();
        fs::write(candidate.join("argocd-ls"), release(VERSION)).unwrap();
    }
    let mut offline = FakeHost::default();
    assert!(distribution::install(&mut offline, &root.0, target(), VERSION).is_err());
    assert!(offline.probes.is_empty());
    let mut online = FakeHost::online(target(), VERSION);
    let repaired = distribution::install(&mut online, &root.0, target(), VERSION).unwrap();
    let mut warm = FakeHost::default();
    assert_eq!(
        distribution::install(&mut warm, &root.0, target(), VERSION).unwrap(),
        repaired
    );
    assert!(warm.downloads.is_empty());
}
