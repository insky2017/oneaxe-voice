// Headless integration entry point; uses exactly the same CLI and library as the app.
#[path = "../src/cli.rs"]
mod cli;

fn main() {
    match cli::run(&std::env::args().collect::<Vec<_>>()) {
        Ok(Some(code)) => std::process::exit(code),
        Ok(None) => {
            eprintln!("请提供 --help 中的诊断命令");
            std::process::exit(2);
        }
        Err(error) => {
            eprintln!("OneAxe Voice Linux：{error}");
            std::process::exit(2);
        }
    }
}
