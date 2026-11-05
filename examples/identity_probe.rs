#[path = "../src/identity.rs"]
mod identity;
use std::io::Read;
fn main() {
    let mut input = Vec::new();
    std::io::stdin().read_to_end(&mut input).unwrap();
    let args: Vec<String> = std::env::args().skip(1).collect();
    let result = match args.as_slice() {
        [] => identity::validate(&input),
        [flag, version] if flag == "--release-version" => {
            identity::validate_release(&input, version)
        }
        _ => Err("Usage: identity_probe [--release-version X.Y.Z] < identity.json".into()),
    };
    match result {
        Ok(()) => println!("accepted"),
        Err(e) => {
            eprintln!("{e}");
            std::process::exit(2)
        }
    }
}
