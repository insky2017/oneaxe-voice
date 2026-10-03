mod cli;
mod tray;
mod ui;

fn main() {
    let args: Vec<String> = std::env::args().collect();
    match cli::run(&args) {
        Ok(Some(code)) => std::process::exit(code),
        Ok(None) => std::process::exit(ui::run(args)),
        Err(error) => {
            eprintln!("OneAxe Voice Linux：{error}");
            std::process::exit(2);
        }
    }
}
