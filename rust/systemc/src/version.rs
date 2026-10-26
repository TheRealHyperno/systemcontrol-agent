//! Release identity, mirroring version.py. The Rust port keeps the Aurelia
//! codename and marks itself as the rewrite.

pub const SYSTEMC_VERSION: &str = "Aurelia-RS";
pub const SYSTEMC_STAGE: &str = "beta";
pub const SYSTEMC_DISPLAY: &str = "Aurelia-RS (beta)";

pub fn banner() -> String {
    format!(
        "You are running a {} version of SystemC codenamed {} — the Rust rewrite. Faster, compiled to a real binary.",
        SYSTEMC_STAGE, SYSTEMC_VERSION
    )
}
