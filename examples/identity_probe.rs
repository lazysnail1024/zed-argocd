#[path = "../src/identity.rs"]
mod identity;
use std::io::Read;
fn main() {
    let mut input=Vec::new();
    std::io::stdin().read_to_end(&mut input).unwrap();
    match identity::validate(&input) {
        Ok(()) => println!("accepted"),
        Err(e) => {eprintln!("{e}");std::process::exit(2)}
    }
}
