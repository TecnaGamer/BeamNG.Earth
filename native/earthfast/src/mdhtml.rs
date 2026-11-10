//! The Add-ons page's text (server/addons.py): an add-on's README and its release notes, Markdown from GitHub, as HTML
//! the game's UI can show. The UI puts it in the page as it is (v-html), so it must be safe by construction: every
//! tag in the output is one of ours, written from the parser's events (pulldown-cmark), and every piece of text is
//! escaped. Raw HTML in the Markdown is dropped (a README's <img>, <details>, <script> ...; a lone <br> stays a line
//! break). Links carry no href: each is <a class="md-link" data-i="k"> with k its index in the returned list of
//! absolute https URLs, and the page opens them through the server (core.openPage, updates.open_page), which only
//! opens a URL from an add-on's own text. Images are not loaded: an image is a link to it, "[Image: alt]".
//!
//! Relative links resolve against the README's place on GitHub (base, e.g.
//! https://github.com/<owner>/<repo>/blob/HEAD/README.md: "../../releases/latest" is the repo's releases page). Only
//! https:// (and "//host" made https) is kept; javascript:, data:, http:, mailto: and in-page anchors ("#...") stay
//! plain text.
//!
//! fold_details: the text up to the first line that starts with "<details" only. An add-on's release notes fold
//! the earlier releases' notes into <details> (Trucking's builds): the page shows those releases on their own.

use pulldown_cmark::{Event, HeadingLevel, Options, Parser, Tag, TagEnd};
use pyo3::prelude::*;

const MAX_URL: usize = 2048;
const MAX_LINKS: usize = 2000;

fn escape_into(out: &mut String, s: &str) {
    for c in s.chars() {
        match c {
            '&' => out.push_str("&amp;"),
            '<' => out.push_str("&lt;"),
            '>' => out.push_str("&gt;"),
            '"' => out.push_str("&quot;"),
            '\'' => out.push_str("&#39;"),
            c if (c as u32) < 0x20 && c != '\n' && c != '\t' => out.push(' '),
            c => out.push(c),
        }
    }
}

/// The text before the first line starting (after spaces) with "<details" (any case).
pub fn fold_details(text: &str) -> &str {
    let mut at = 0;
    for line in text.split_inclusive('\n') {
        let t = line.trim_start();
        if t.len() >= 8 && t[..8].eq_ignore_ascii_case("<details") {
            return &text[..at];
        }
        at += line.len();
    }
    text
}

fn url_ok(u: &str) -> bool {
    u.len() <= MAX_URL
        && u.starts_with("https://")
        && u.len() > 8
        && !u.chars().any(|c| c.is_whitespace() || c.is_control() || "\"'<>`\\{}|^".contains(c))
}

/// "a/b/../c/./d" -> "a/c/d" (the path's dot segments; a leading "/" kept).
fn remove_dots(path: &str) -> String {
    let mut out: Vec<&str> = Vec::new();
    let segs: Vec<&str> = path.split('/').collect();
    for (i, s) in segs.iter().enumerate() {
        match *s {
            "." => {
                if i == segs.len() - 1 {
                    out.push("");
                }
            }
            ".." => {
                if out.len() > 1 {
                    out.pop();
                }
                if i == segs.len() - 1 {
                    out.push("");
                }
            }
            s => out.push(s),
        }
    }
    out.join("/")
}

/// The absolute https URL of a link's destination in a document at base (an https URL), or None.
pub fn resolve(dest: &str, base: &str) -> Option<String> {
    let d = dest.trim();
    if d.is_empty() || d.starts_with('#') {
        return None;
    }
    if d.starts_with("//") {
        let u = format!("https:{d}");
        return url_ok(&u).then_some(u);
    }
    // a scheme: letters, digits, + - . before the first ':' (and before any '/', '?', '#')
    if let Some(colon) = d.find(':') {
        let head = &d[..colon];
        if !head.is_empty() && head.chars().all(|c| c.is_ascii_alphanumeric() || "+-.".contains(c)) {
            return (head.eq_ignore_ascii_case("https") && url_ok(d)).then(|| format!("https{}", &d[colon..]));
        }
    }
    if !url_ok(base) {
        return None;
    }
    let rest = &base[8..];
    let host_end = rest.find('/').map(|i| i + 8).unwrap_or(base.len());
    let origin = &base[..host_end];
    let base_path = base[host_end..].split(['?', '#']).next().unwrap_or("");
    let (dpath, tail) = match d.find(['?', '#']) {
        Some(i) => (&d[..i], &d[i..]),
        None => (d, ""),
    };
    let joined = if dpath.starts_with('/') {
        dpath.to_string()
    } else {
        let dir = match base_path.rfind('/') {
            Some(i) => &base_path[..=i],
            None => "/",
        };
        format!("{dir}{dpath}")
    };
    let mut path = remove_dots(&joined);
    if !path.starts_with('/') {
        path.insert(0, '/');
    }
    let u = format!("{origin}{path}{tail}");
    url_ok(&u).then_some(u)
}

fn heading_n(l: HeadingLevel) -> u8 {
    match l {
        HeadingLevel::H1 => 1,
        HeadingLevel::H2 => 2,
        HeadingLevel::H3 => 3,
        HeadingLevel::H4 => 4,
        HeadingLevel::H5 => 5,
        HeadingLevel::H6 => 6,
    }
}

fn is_br(html: &str) -> bool {
    let t = html.trim().to_ascii_lowercase();
    t == "<br>" || t == "<br/>" || t == "<br />"
}

struct Out {
    html: String,
    open: Vec<&'static str>,   // the tags open now, to close them all at a cut
    links: Vec<String>,
}

impl Out {
    fn open(&mut self, tag: &'static str, attrs: &str) {
        self.html.push('<');
        self.html.push_str(tag);
        self.html.push_str(attrs);
        self.html.push('>');
        self.open.push(tag);
    }
    fn close(&mut self, tag: &'static str) {
        // (the parser's events nest; a tag it ends that is not ours, never: closed only when it is the innermost)
        if self.open.last() == Some(&tag) {
            self.open.pop();
            self.html.push_str("</");
            self.html.push_str(tag);
            self.html.push('>');
        }
    }
    fn close_all(&mut self) {
        while let Some(t) = self.open.pop() {
            self.html.push_str("</");
            self.html.push_str(t);
            self.html.push('>');
        }
    }
    fn link(&mut self, url: String) -> usize {
        if let Some(i) = self.links.iter().position(|u| *u == url) {
            return i;
        }
        self.links.push(url);
        self.links.len() - 1
    }
}

/// Text with its bare https:// addresses as links (GitHub links them too: "Join the Discord: https://discord.gg/x").
/// An address ends at a space or a quote / bracket; a closing . , ; : ! ? ) after it is the sentence's, not the
/// address's.
fn text_autolinks(o: &mut Out, t: &str) {
    let mut rest = t;
    while let Some(i) = rest.find("https://") {
        escape_into(&mut o.html, &rest[..i]);
        let tail = &rest[i..];
        let mut end = tail.find(|c: char| c.is_whitespace() || "\"'<>`".contains(c)).unwrap_or(tail.len());
        while end > 8 && tail[..end].ends_with(['.', ',', ';', ':', '!', '?', ')']) {
            end -= 1;
        }
        let addr = &tail[..end];
        match (o.links.len() < MAX_LINKS).then(|| resolve(addr, "")).flatten() {
            Some(u) if end > 8 => {
                let k = o.link(u);
                o.html.push_str(&format!("<a class=\"md-link\" data-i=\"{k}\">"));
                escape_into(&mut o.html, addr);
                o.html.push_str("</a>");
            }
            _ => escape_into(&mut o.html, addr),
        }
        rest = &tail[end.max(1)..];
        if end == 0 {
            // (never: "https://" is 8 characters)
            break;
        }
    }
    escape_into(&mut o.html, rest);
}

/// (html, links) of Markdown text (see the module's notes); max_out: the HTML's length at most (bytes, then cut with
/// a closing "..." paragraph).
pub fn render(text: &str, base: &str, fold: bool, max_out: usize) -> (String, Vec<String>) {
    let text = if fold { fold_details(text) } else { text };
    let opts = Options::ENABLE_TABLES | Options::ENABLE_STRIKETHROUGH | Options::ENABLE_TASKLISTS;
    let mut o = Out { html: String::with_capacity(text.len() + text.len() / 4), open: Vec::new(), links: Vec::new() };
    // inside an image: its alt text is gathered, then one link stands for it
    let mut img: Option<(Option<String>, String)> = None;
    let mut table_head = false;
    let mut link_open: Vec<bool> = Vec::new();     // per open link: whether it became an <a> (else plain text)
    for ev in Parser::new_ext(text, opts) {
        if o.html.len() > max_out {
            o.close_all();
            o.html.push_str("<p class=\"md-cut\">...</p>");
            break;
        }
        if let Some((_, alt)) = img.as_mut() {
            match ev {
                Event::Text(t) | Event::Code(t) => {
                    alt.push_str(&t);
                    continue;
                }
                Event::End(TagEnd::Image) => {}
                _ => continue,
            }
        }
        match ev {
            Event::Start(tag) => match tag {
                Tag::Paragraph => o.open("p", ""),
                Tag::Heading { level, .. } => o.open(["h1", "h2", "h3", "h4", "h5", "h6"][heading_n(level) as usize - 1], ""),
                Tag::BlockQuote(_) => o.open("blockquote", ""),
                Tag::CodeBlock(_) => {
                    o.open("pre", "");
                    o.open("code", "");
                }
                Tag::List(Some(start)) => {
                    let a = if start == 1 { String::new() } else { format!(" start=\"{}\"", start.min(1_000_000)) };
                    o.open("ol", &a)
                }
                Tag::List(None) => o.open("ul", ""),
                Tag::Item => o.open("li", ""),
                Tag::Table(_) => o.open("table", ""),
                Tag::TableHead => {
                    table_head = true;
                    o.open("thead", "");
                    o.open("tr", "");
                }
                Tag::TableRow => o.open("tr", ""),
                Tag::TableCell => o.open(if table_head { "th" } else { "td" }, ""),
                Tag::Emphasis => o.open("em", ""),
                Tag::Strong => o.open("strong", ""),
                Tag::Strikethrough => o.open("del", ""),
                Tag::Link { dest_url, .. } => {
                    let url = if o.links.len() < MAX_LINKS { resolve(&dest_url, base) } else { None };
                    match url {
                        Some(u) => {
                            let i = o.link(u);
                            o.open("a", &format!(" class=\"md-link\" data-i=\"{i}\""));
                            link_open.push(true);
                        }
                        None => link_open.push(false),
                    }
                }
                Tag::Image { dest_url, .. } => {
                    let url = if o.links.len() < MAX_LINKS { resolve(&dest_url, base) } else { None };
                    img = Some((url, String::new()));
                }
                // footnotes, definition lists, metadata, HTML blocks: their text as plain text, no tag
                _ => {}
            },
            Event::End(tag) => match tag {
                TagEnd::Paragraph => o.close("p"),
                TagEnd::Heading(level) => o.close(["h1", "h2", "h3", "h4", "h5", "h6"][heading_n(level) as usize - 1]),
                TagEnd::BlockQuote(_) => o.close("blockquote"),
                TagEnd::CodeBlock => {
                    o.close("code");
                    o.close("pre");
                }
                TagEnd::List(true) => o.close("ol"),
                TagEnd::List(false) => o.close("ul"),
                TagEnd::Item => o.close("li"),
                TagEnd::Table => {
                    o.close("tbody");
                    o.close("table");
                }
                TagEnd::TableHead => {
                    table_head = false;
                    o.close("tr");
                    o.close("thead");
                    o.open("tbody", "");
                }
                TagEnd::TableRow => o.close("tr"),
                TagEnd::TableCell => {
                    if o.open.last() == Some(&"th") {
                        o.close("th")
                    } else {
                        o.close("td")
                    }
                }
                TagEnd::Emphasis => o.close("em"),
                TagEnd::Strong => o.close("strong"),
                TagEnd::Strikethrough => o.close("del"),
                TagEnd::Link => {
                    if link_open.pop() == Some(true) {
                        o.close("a")
                    }
                }
                TagEnd::Image => {
                    if let Some((url, alt)) = img.take() {
                        let label = if alt.trim().is_empty() { "Image".to_string() } else { format!("Image: {}", alt.trim()) };
                        match url {
                            Some(u) => {
                                let i = o.link(u);
                                o.html.push_str(&format!("<a class=\"md-link md-img\" data-i=\"{i}\">["));
                                escape_into(&mut o.html, &label);
                                o.html.push_str("]</a>");
                            }
                            None => {
                                o.html.push('[');
                                escape_into(&mut o.html, &label);
                                o.html.push(']');
                            }
                        }
                    }
                }
                _ => {}
            },
            Event::Text(t) => {
                if link_open.iter().any(|&a| a) || o.open.last() == Some(&"code") {
                    escape_into(&mut o.html, &t)
                } else {
                    text_autolinks(&mut o, &t)
                }
            }
            Event::Code(t) => {
                o.html.push_str("<code>");
                escape_into(&mut o.html, &t);
                o.html.push_str("</code>");
            }
            Event::InlineHtml(h) | Event::Html(h) => {
                if is_br(&h) {
                    o.html.push_str("<br>");
                }
            }
            Event::SoftBreak => o.html.push(' '),
            Event::HardBreak => o.html.push_str("<br>"),
            Event::Rule => o.html.push_str("<hr>"),
            Event::TaskListMarker(done) => o.html.push_str(if done { "[x] " } else { "[ ] " }),
            Event::FootnoteReference(t) => {
                o.html.push('[');
                escape_into(&mut o.html, &t);
                o.html.push(']');
            }
            _ => {}
        }
    }
    o.close_all();
    (o.html, o.links)
}

/// md_html(text, base, fold_details=False, max_out=262144) -> (html, [url, ...]): see the module's notes.
#[pyfunction]
#[pyo3(signature = (text, base, fold_details = false, max_out = 262144))]
pub fn md_html(py: Python<'_>, text: &str, base: &str, fold_details: bool, max_out: usize) -> (String, Vec<String>) {
    py.allow_threads(|| render(text, base, fold_details, max_out))
}

#[cfg(test)]
mod tests {
    use super::*;

    const BASE: &str = "https://github.com/o/r/blob/HEAD/README.md";

    fn html(md: &str) -> (String, Vec<String>) {
        render(md, BASE, false, 1 << 20)
    }

    #[test]
    fn raw_html_and_scripts_never_pass() {
        let (h, _) = html("<script>alert(1)</script>\n\nhi <b onclick=\"x()\">there</b>\n\n<img src=x onerror=alert(1)>");
        assert!(!h.contains("<script") && !h.contains("onclick") && !h.contains("onerror") && !h.contains("<img"), "{h}");
        assert!(h.contains("hi there"), "{h}");
        let (h, _) = html("a <br> b");
        assert!(h.contains("a <br> b"), "{h}");
    }

    #[test]
    fn text_is_escaped() {
        let (h, _) = html("1 < 2 & \"q\" 'a' `<x>`");
        assert!(h.contains("1 &lt; 2 &amp; &quot;q&quot; &#39;a&#39; <code>&lt;x&gt;</code>"), "{h}");
    }

    #[test]
    fn links_only_https_and_no_href() {
        let (h, l) = html("[a](javascript:alert(1)) [b](data:text/html,x) [c](http://x.com) [d](https://ok.example/p?q=1#f) \
                          [e](#local) [f](mailto:a@b.c) [g](JaVaScRiPt:x)");
        assert_eq!(l, vec!["https://ok.example/p?q=1#f".to_string()]);
        assert!(!h.contains("href") && !h.contains("javascript"), "{h}");
        assert!(h.contains("<a class=\"md-link\" data-i=\"0\">d</a>"), "{h}");
        assert!(h.contains("<p>a b c <a"), "the refused links stay text: {h}");
    }

    #[test]
    fn relative_links_resolve_on_github() {
        assert_eq!(resolve("../../releases/latest", BASE).unwrap(), "https://github.com/o/r/releases/latest");
        assert_eq!(resolve("LICENSE", BASE).unwrap(), "https://github.com/o/r/blob/HEAD/LICENSE");
        assert_eq!(resolve("../../issues", BASE).unwrap(), "https://github.com/o/r/issues");
        assert_eq!(resolve("/x/y", BASE).unwrap(), "https://github.com/x/y");
        assert_eq!(resolve("//cdn.example/a", BASE).unwrap(), "https://cdn.example/a");
        assert_eq!(resolve("docs/a.md#part", BASE).unwrap(), "https://github.com/o/r/blob/HEAD/docs/a.md#part");
        assert!(resolve("../../../../../../etc", BASE).unwrap().starts_with("https://github.com/"));
        assert!(resolve("https://a.example/\"onx", BASE).is_none());
        assert!(resolve("https://a.example/ x", BASE).is_none());
        assert!(resolve("x", "javascript:alert(1)").is_none());
    }

    #[test]
    fn images_are_links_not_loaded() {
        let (h, l) = html("![a & <b>car</b>](https://img.example/c.png) ![](rel.png)");
        assert!(!h.contains("<img") && !h.contains("src="), "{h}");
        assert!(h.contains("[Image: a &amp; car]") && h.contains("[Image]"), "{h}");
        assert_eq!(l[0], "https://img.example/c.png");
        assert_eq!(l[1], "https://github.com/o/r/blob/HEAD/rel.png");
    }

    #[test]
    fn tables_lists_code_headings() {
        let (h, _) = html("# T\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n3. x\n4. y\n\n- [x] done\n\n```\n<b>\n```\n");
        for want in ["<h1>T</h1>", "<thead><tr><th>a</th><th>b</th></tr></thead><tbody><tr><td>1</td><td>2</td></tr></tbody>",
                     "<ol start=\"3\"><li>x</li>", "[x] done", "<pre><code>&lt;b&gt;\n</code></pre>"] {
            assert!(h.contains(want), "{want} in {h}");
        }
    }

    #[test]
    fn fold_details_keeps_the_newest_notes() {
        let t = "## What's changed\n\n- new\n\n<details>\n<summary>Earlier builds</summary>\n\n### Build 1\n- old\n</details>\n";
        let (h, _) = render(t, BASE, true, 1 << 20);
        assert!(h.contains("new") && !h.contains("old") && !h.contains("Earlier"), "{h}");
        let (h, _) = render(t, BASE, false, 1 << 20);
        assert!(h.contains("old") && !h.contains("<details") && !h.contains("<summary"), "{h}");
    }

    #[test]
    fn cut_closes_every_tag() {
        let md = "- a **b ".to_string() + &"word ".repeat(5000) + "**";
        let (h, _) = render(&md, BASE, false, 1000);
        assert!(h.ends_with("<p class=\"md-cut\">...</p>"), "{}", &h[h.len() - 80..]);
        let opens = h.matches("<li>").count();
        assert_eq!(opens, h.matches("</li>").count());
        assert_eq!(h.matches("<ul>").count(), h.matches("</ul>").count());
    }

    #[test]
    fn bare_addresses_become_links() {
        let (h, l) = html("Join: https://discord.gg/abc. Or (https://a.example/x?y=1), not `https://code.example` \
                          nor [t](https://b.example/) nor https://bad.example/\"x");
        assert_eq!(l[0], "https://discord.gg/abc");
        assert_eq!(l[1], "https://a.example/x?y=1");
        assert!(h.contains("<a class=\"md-link\" data-i=\"0\">https://discord.gg/abc</a>. Or ("), "{h}");
        assert!(h.contains("<code>https://code.example</code>"), "{h}");
        assert!(l.contains(&"https://b.example/".to_string()) && l.contains(&"https://bad.example/".to_string()), "{l:?}");
        assert!(!h.contains("href"), "{h}");
    }

    #[test]
    fn nested_markup_in_links() {
        let (h, l) = html("[**bold** link](https://a.example/) and [*x*](javascript:y)");
        assert!(h.contains("<a class=\"md-link\" data-i=\"0\"><strong>bold</strong> link</a>"), "{h}");
        assert!(h.contains("<em>x</em>") && l.len() == 1, "{h}");
    }
}
