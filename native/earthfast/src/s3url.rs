//! Public S3 sources use regional dual-stack endpoints at the transport boundary. Source URLs remain cache,
//! quota and circuit-breaker identities. Only known buckets are rewritten; signed/private URLs are untouched.

pub fn dualstack_url(url: &str, authorized: bool) -> String {
    if authorized { return url.to_owned(); }
    let Some(rest) = url.strip_prefix("https://") else { return url.to_owned(); };
    let end = rest.find(['/', '?', '#']).unwrap_or(rest.len());
    let host = rest[..end].to_ascii_lowercase();
    let suffix = &rest[end..];
    if let Some((_, query)) = suffix.split_once('?') {
        if query.split('#').next().unwrap_or("").split('&').any(|part| {
            let key = part.split('=').next().unwrap_or("").to_ascii_lowercase();
            matches!(key.as_str(), "x-amz-signature" | "x-amz-credential" | "x-amz-algorithm" |
                     "awsaccesskeyid" | "signature")
        }) { return url.to_owned(); }
    }
    let target = match host.as_str() {
        "overturemaps-us-west-2.s3.us-west-2.amazonaws.com" => "overturemaps-us-west-2.s3.dualstack.us-west-2.amazonaws.com",
        "esa-worldcover.s3.eu-central-1.amazonaws.com" => "esa-worldcover.s3.dualstack.eu-central-1.amazonaws.com",
        "canelevation-dem.s3.ca-central-1.amazonaws.com" => "canelevation-dem.s3.dualstack.ca-central-1.amazonaws.com",
        "copernicus-dem-30m.s3.amazonaws.com" => "copernicus-dem-30m.s3.dualstack.eu-central-1.amazonaws.com",
        "dataforgood-fb-data.s3.amazonaws.com" => "dataforgood-fb-data.s3.dualstack.us-east-1.amazonaws.com",
        "s3-us-west-2.amazonaws.com" if suffix.starts_with("/usgs-lidar-public/") => "s3.dualstack.us-west-2.amazonaws.com",
        "s3.amazonaws.com" if suffix.starts_with("/elevation-tiles-prod/") => "s3.dualstack.us-east-1.amazonaws.com",
        _ => return url.to_owned(),
    };
    format!("https://{target}{suffix}")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn known_public_sources_keep_object_and_query() {
        for (source, target) in [
            ("overturemaps-us-west-2.s3.us-west-2", "overturemaps-us-west-2.s3.dualstack.us-west-2"),
            ("esa-worldcover.s3.eu-central-1", "esa-worldcover.s3.dualstack.eu-central-1"),
            ("canelevation-dem.s3.ca-central-1", "canelevation-dem.s3.dualstack.ca-central-1"),
            ("copernicus-dem-30m.s3", "copernicus-dem-30m.s3.dualstack.eu-central-1"),
            ("dataforgood-fb-data.s3", "dataforgood-fb-data.s3.dualstack.us-east-1"),
        ] {
            assert_eq!(dualstack_url(&format!("https://{source}.amazonaws.com/a%20b/part.parquet?versionId=abc%2Fdef"), false),
                       format!("https://{target}.amazonaws.com/a%20b/part.parquet?versionId=abc%2Fdef"));
        }
        assert_eq!(dualstack_url("https://s3.amazonaws.com/elevation-tiles-prod/terrarium/0/0/0.png", false),
                   "https://s3.dualstack.us-east-1.amazonaws.com/elevation-tiles-prod/terrarium/0/0/0.png");
        assert_eq!(dualstack_url("https://s3-us-west-2.amazonaws.com/usgs-lidar-public/ept.json", false),
                   "https://s3.dualstack.us-west-2.amazonaws.com/usgs-lidar-public/ept.json");
    }

    #[test]
    fn leaves_unrelated_signed_and_authorized_urls_unchanged() {
        for url in ["http://copernicus-dem-30m.s3.amazonaws.com/a", "https://s3.amazonaws.com/other/a",
                    "https://s3-us-west-2.amazonaws.com/other/a", "https://unknown.s3.amazonaws.com/a",
                    "https://copernicus-dem-30m.s3.amazonaws.com.evil.example/a",
                    "https://copernicus-dem-30m.s3.amazonaws.com:8443/a",
                    "https://copernicus-dem-30m.s3.dualstack.eu-central-1.amazonaws.com/a",
                    "https://copernicus-dem-30m.s3.amazonaws.com/a?X-Amz-Signature=abc",
                    "https://copernicus-dem-30m.s3.amazonaws.com/a?AWSAccessKeyId=x&Signature=abc"] {
            assert_eq!(dualstack_url(url, false), url);
        }
        let url = "https://copernicus-dem-30m.s3.amazonaws.com/a";
        assert_eq!(dualstack_url(url, true), url);
    }
}
