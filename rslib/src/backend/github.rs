// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

use std::io;
use std::io::Read;
use std::time::Duration;

use anki_io::open_file;
use anki_io::read_dir_files;
use anki_io::remove_file;
use anki_proto::github::GithubRelease;
use anki_proto::github::LatestReleaseRequest;
use serde_json::Value;
use sha2::Digest;

use super::Backend;
use crate::prelude::*;
use crate::services::BackendGithubService;
use crate::updates::download_file;
use crate::updates::release_path;
use crate::updates::reqwest_error_to_anki_error;
use crate::updates::updates_dir;
use crate::updates::user_agent;
use crate::updates::DownloadUpdateProgress;

const ALL_RELEASES_URL: &str = "https://api.github.com/repos/Expertium/Clanki/releases";
const LATEST_RELEASE_URL: &str = "https://api.github.com/repos/Expertium/Clanki/releases/latest";

// NOTE: must match installer filenames produced by build_installer.py
fn get_platform_installer_suffix() -> Option<&'static str> {
    match (std::env::consts::OS, std::env::consts::ARCH) {
        ("windows", "x86_64") => Some("-win-x64.msi"),
        ("windows", "aarch64") => Some("-win-arm64.msi"),
        ("macos", "x86_64") => Some("-mac-intel.dmg"),
        ("macos", "aarch64") => Some("-mac-apple.dmg"),
        ("linux", "x86_64") => Some("-linux-x86_64.tar.zst"),
        ("linux", "aarch64") => Some("-linux-aarch64.tar.zst"),
        _ => None,
    }
}

fn find_platform_installer_asset<'a>(
    assets: &'a [Value],
    installer_suffix: &str,
) -> Option<&'a Value> {
    assets.iter().find(|asset| {
        asset["name"].as_str().is_some_and(|filename| {
            !filename.contains("-portable-") && filename.ends_with(installer_suffix)
        })
    })
}

/// Fetches the raw release JSON from `url`. A repository with zero releases
/// is reported the same way as "no update available" (via `no_updates_msg`),
/// never as an error: GitHub 404s `/releases/latest` and 200s `/releases`
/// with `[]` when there is nothing to return.
async fn fetch_release_info(
    client: &reqwest::Client,
    url: &str,
    include_prerelease: bool,
    no_updates_msg: &str,
) -> Result<Value> {
    let response = client
        .get(url)
        .header("User-Agent", user_agent())
        .timeout(Duration::from_secs(60))
        .send()
        .await
        .map_err(reqwest_error_to_anki_error)?;
    if response.status() == reqwest::StatusCode::NOT_FOUND {
        invalid_input!("{}", no_updates_msg);
    }
    let response = response
        .error_for_status()
        .map_err(reqwest_error_to_anki_error)?;
    if include_prerelease {
        let json: Value = response.json().await?;
        let releases = json.as_array().or_invalid("expected an array")?;
        Ok(releases.first().or_invalid(no_updates_msg)?.clone())
    } else {
        Ok(response.json().await?)
    }
}

fn release_is_downloaded(filename: &str, checksum: &str) -> Result<bool> {
    let output_path = release_path(filename)?;
    if output_path.exists() {
        let mut buf = [0; 64 * 1024];
        let mut file = open_file(&output_path)?;
        let mut digest = sha2::Sha256::new();
        loop {
            let count = match file.read(&mut buf) {
                Ok(0) => break,
                Err(e) if e.kind() == io::ErrorKind::Interrupted => continue,
                result => result?,
            };
            digest.update(&buf[..count]);
        }
        let actual_checksum = hex::encode(digest.finalize());

        Ok(actual_checksum == checksum)
    } else {
        Ok(false)
    }
}

impl BackendGithubService for Backend {
    fn get_latest_release(&self, input: LatestReleaseRequest) -> Result<GithubRelease> {
        let no_updates_msg = self.tr.errors_no_updates_available();
        let installer_suffix =
            get_platform_installer_suffix().or_invalid(no_updates_msg.clone())?;
        let url = if input.include_prerelease {
            ALL_RELEASES_URL
        } else {
            LATEST_RELEASE_URL
        };
        self.runtime_handle().block_on(async {
            let release_info = fetch_release_info(
                &self.web_client(),
                url,
                input.include_prerelease,
                &no_updates_msg,
            )
            .await?;
            let tag_name = release_info["tag_name"]
                .as_str()
                .or_invalid("release tag not found")?;
            let target_commitish = release_info["target_commitish"]
                .as_str()
                .or_invalid("release target commit not found")?;
            let assets = release_info["assets"]
                .as_array()
                .or_invalid("assets should be an array")?;
            let asset = find_platform_installer_asset(assets, installer_suffix)
                .or_invalid(no_updates_msg)?;
            let filename = asset["name"]
                .as_str()
                .or_invalid("release name not found")?;
            let url = asset["browser_download_url"]
                .as_str()
                .or_invalid("download URL not found")?;
            let checksum = asset["digest"]
                .as_str()
                .or_invalid("release digest not found")?
                .split_once("sha256:")
                .or_invalid("sha256 suffix not found")?
                .1;
            Ok(GithubRelease {
                tag_name: tag_name.into(),
                filename: filename.into(),
                url: url.into(),
                checksum: checksum.into(),
                target_commitish: target_commitish.into(),
            })
        })
    }

    fn download_release(&self, release: GithubRelease) -> Result<anki_proto::generic::String> {
        let mut progress = self.new_progress_handler::<DownloadUpdateProgress>();
        let already_downloaded = release_is_downloaded(&release.filename, &release.checksum)?;
        if !already_downloaded {
            self.runtime_handle().block_on(async {
                download_file(
                    &self.web_client(),
                    &mut progress,
                    &release.filename,
                    &release.url,
                    &release.checksum,
                )
                .await
            })?;
        }

        // Remove old downloads
        let output_dir = updates_dir()?;
        let output_path = release_path(&release.filename)?;
        for file in read_dir_files(output_dir)? {
            let path = file?.path();
            if path != output_path {
                let _ = remove_file(path);
            }
        }

        let output_path = output_path
            .to_str()
            .or_invalid("non-unicode filename")?
            .to_string();
        Ok(output_path.into())
    }
}

#[cfg(test)]
mod tests {
    use serde_json::json;
    use wiremock::matchers::method;
    use wiremock::Mock;
    use wiremock::MockServer;
    use wiremock::ResponseTemplate;

    use super::*;

    const NO_UPDATES: &str = "No updates available.";

    fn assert_is_no_updates_error(err: AnkiError) {
        match err {
            AnkiError::InvalidInput { source } => assert_eq!(source.message(), NO_UPDATES),
            other => panic!("expected the \"no updates\" InvalidInput error, got {other:?}"),
        }
    }

    /// Pins spec/updates.md#updates.dev-build-always-offered's sibling rule
    /// in updates.release-source: a repository with zero releases is
    /// reported as "no update available", not as an error. GitHub 404s
    /// `/releases/latest` when there are no releases at all.
    #[tokio::test]
    async fn no_releases_at_all_is_reported_as_no_updates() {
        let mock_server = MockServer::start().await;
        Mock::given(method("GET"))
            .respond_with(ResponseTemplate::new(404))
            .mount(&mock_server)
            .await;

        let err = fetch_release_info(
            &reqwest::Client::new(),
            &mock_server.uri(),
            false,
            NO_UPDATES,
        )
        .await
        .unwrap_err();
        assert_is_no_updates_error(err);
    }

    /// Same rule, for the `include_prerelease` path: GitHub 200s `/releases`
    /// with an empty array instead of 404ing.
    #[tokio::test]
    async fn empty_releases_array_is_reported_as_no_updates() {
        let mock_server = MockServer::start().await;
        Mock::given(method("GET"))
            .respond_with(ResponseTemplate::new(200).set_body_json(json!([])))
            .mount(&mock_server)
            .await;

        let err = fetch_release_info(
            &reqwest::Client::new(),
            &mock_server.uri(),
            true,
            NO_UPDATES,
        )
        .await
        .unwrap_err();
        assert_is_no_updates_error(err);
    }

    /// A real failure must still surface as an error and must NOT be
    /// reported as "no update available" — only the absence of releases is
    /// special-cased.
    #[tokio::test]
    async fn other_failures_are_not_reported_as_no_updates() {
        let mock_server = MockServer::start().await;
        Mock::given(method("GET"))
            .respond_with(ResponseTemplate::new(500))
            .mount(&mock_server)
            .await;

        let err = fetch_release_info(
            &reqwest::Client::new(),
            &mock_server.uri(),
            false,
            NO_UPDATES,
        )
        .await
        .unwrap_err();
        assert!(!matches!(
            &err,
            AnkiError::InvalidInput { source } if source.message() == NO_UPDATES
        ));
    }

    /// Sanity check that the refactor to extract [fetch_release_info] kept
    /// the ordinary, non-empty case working.
    #[tokio::test]
    async fn returns_the_first_release_when_prereleases_are_included() {
        let mock_server = MockServer::start().await;
        Mock::given(method("GET"))
            .respond_with(ResponseTemplate::new(200).set_body_json(json!([
                { "tag_name": "26.10" },
                { "tag_name": "26.09" },
            ])))
            .mount(&mock_server)
            .await;

        let release_info = fetch_release_info(
            &reqwest::Client::new(),
            &mock_server.uri(),
            true,
            NO_UPDATES,
        )
        .await
        .unwrap();
        assert_eq!(release_info["tag_name"].as_str(), Some("26.10"));
    }

    /// Pins spec/updates.md#updates.release-source: Clanki must never offer
    /// another project's releases as an update to itself.
    #[test]
    fn release_urls_point_at_clanki() {
        assert!(ALL_RELEASES_URL.contains("/repos/Expertium/Clanki/"));
        assert!(LATEST_RELEASE_URL.contains("/repos/Expertium/Clanki/"));
    }

    #[test]
    fn update_asset_selection_ignores_portable_downloads() {
        for (installer_suffix, installer, portable) in [
            (
                "-win-x64.msi",
                "anki-1.2.3-win-x64.msi",
                "anki-1.2.3-portable-win-x64.zip",
            ),
            (
                "-win-arm64.msi",
                "anki-1.2.3-win-arm64.msi",
                "anki-1.2.3-portable-win-arm64.zip",
            ),
            (
                "-mac-apple.dmg",
                "anki-1.2.3-mac-apple.dmg",
                "anki-1.2.3-portable-mac-apple.zip",
            ),
            (
                "-mac-intel.dmg",
                "anki-1.2.3-mac-intel.dmg",
                "anki-1.2.3-portable-mac-intel.zip",
            ),
            (
                "-linux-x86_64.tar.zst",
                "anki-1.2.3-linux-x86_64.tar.zst",
                "anki-1.2.3-portable-linux-x86_64.tar.zst",
            ),
            (
                "-linux-aarch64.tar.zst",
                "anki-1.2.3-linux-aarch64.tar.zst",
                "anki-1.2.3-portable-linux-aarch64.tar.zst",
            ),
        ] {
            let release_assets = json!([
                { "name": portable },
                { "name": installer },
            ]);
            let selected =
                find_platform_installer_asset(release_assets.as_array().unwrap(), installer_suffix)
                    .unwrap();

            assert_eq!(selected["name"].as_str(), Some(installer));
        }
    }

    #[test]
    fn portable_download_is_not_an_installer_fallback() {
        let release_assets = json!([
            { "name": "anki-1.2.3-portable-win-x64.msi" },
        ]);

        assert!(
            find_platform_installer_asset(release_assets.as_array().unwrap(), "-win-x64.msi",)
                .is_none()
        );
    }
}
