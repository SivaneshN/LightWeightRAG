#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::fs::{self, OpenOptions};
use std::io::{Read, Write};
use std::net::{TcpListener, TcpStream};
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant};
use tauri::webview::WebviewWindowBuilder;
use tauri::{Manager, WebviewUrl, WindowEvent};

const BACKEND_PORT: u16 = 38571;

#[cfg(target_os = "windows")]
const BACKEND_NAME: &str = "LightWeightRAG-backend.exe";
#[cfg(not(target_os = "windows"))]
const BACKEND_NAME: &str = "LightWeightRAG-backend";

struct BackendProcess {
    child: Arc<Mutex<Option<Child>>>,
    closing: Arc<AtomicBool>,
    token: Arc<Mutex<Option<String>>>,
}

fn local_backend_url(port: u16, token: &str) -> Result<url::Url, String> {
    url::Url::parse(&format!("http://127.0.0.1:{port}/?desktop_token={token}"))
        .map_err(|error| format!("Could not prepare the local app window: {error}"))
}

fn start_backend(app: &tauri::AppHandle, token: &str) -> Result<Child, String> {
    if TcpListener::bind(("127.0.0.1", BACKEND_PORT)).is_err() {
        return Err("Another local service is using the app's private port. Close any other LightWeightRAG window and try again.".into());
    }

    let resource_dir = app
        .path()
        .resource_dir()
        .map_err(|error| format!("Could not find the app files: {error}"))?;
    let backend = resource_dir
        .join("resources")
        .join("backend")
        .join(BACKEND_NAME);
    let app_data = app
        .path()
        .app_data_dir()
        .map_err(|error| format!("Could not find the app data folder: {error}"))?;
    fs::create_dir_all(&app_data)
        .map_err(|error| format!("Could not create the private app data folder: {error}"))?;
    let log_dir = app_data.join("logs");
    fs::create_dir_all(&log_dir)
        .map_err(|error| format!("Could not create the app log folder: {error}"))?;
    let log_path = log_dir.join("local-service.log");
    let log = OpenOptions::new()
        .create(true)
        .append(true)
        .open(&log_path)
        .map_err(|error| format!("Could not open the app log: {error}"))?;
    let stdout = log
        .try_clone()
        .map_err(|error| format!("Could not prepare the app log: {error}"))?;

    let mut command = if backend.is_file() {
        Command::new(&backend)
    } else if cfg!(debug_assertions) {
        let project_root = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../..");
        let entrypoint = project_root.join("desktop_backend.py");
        let python = if cfg!(target_os = "windows") { "python" } else { "python3" };
        let mut dev_command = Command::new(python);
        dev_command.arg(entrypoint).current_dir(project_root);
        dev_command
    } else {
        return Err(format!(
            "The local service is missing. Reinstall LightWeightRAG and try again. Details are saved in {}.",
            log_path.display()
        ));
    };

    command
        .arg("--host")
        .arg("127.0.0.1")
        .arg("--port")
        .arg(BACKEND_PORT.to_string())
        .env("LIGHTWEIGHTRAG_DATA_DIR", &app_data)
        .env("LIGHTWEIGHTRAG_DESKTOP_TOKEN", token)
        .stdin(Stdio::null())
        .stdout(Stdio::from(stdout))
        .stderr(Stdio::from(log));

    command
        .spawn()
        .map_err(|error| format!("Could not start the local research service: {error}. Details are saved in {}.", log_path.display()))
}

fn wait_for_backend(child: &mut Child, token: &str) -> Result<(), String> {
    let deadline = Instant::now() + Duration::from_secs(45);
    while Instant::now() < deadline {
        if let Some(status) = child
            .try_wait()
            .map_err(|error| format!("Could not check the local service: {error}"))?
        {
            return Err(format!("The local research service stopped during startup ({status})."));
        }

        if let Ok(mut stream) = TcpStream::connect(("127.0.0.1", BACKEND_PORT)) {
            let _ = stream.set_read_timeout(Some(Duration::from_millis(500)));
            let request = format!(
                "GET /api/projects HTTP/1.0\r\nHost: 127.0.0.1\r\nX-LightWeightRAG-Desktop-Token: {token}\r\n\r\n"
            );
            let _ = stream.write_all(request.as_bytes());
            let mut response = [0_u8; 128];
            if let Ok(count) = stream.read(&mut response) {
                let response = String::from_utf8_lossy(&response[..count]);
                if response.starts_with("HTTP/1.1 200") || response.starts_with("HTTP/1.0 200") {
                    return Ok(());
                }
            }
        }
        thread::sleep(Duration::from_millis(250));
    }
    Err("The local research service took too long to start. Close the app and try again.".into())
}

fn stop_backend(child: &mut Child, token: &str) {
    let address = std::net::SocketAddr::from(([127, 0, 0, 1], BACKEND_PORT));
    if let Ok(mut stream) = TcpStream::connect_timeout(&address, Duration::from_millis(300)) {
        let request = format!(
            "POST /api/desktop/shutdown HTTP/1.1\r\nHost: 127.0.0.1\r\nX-LightWeightRAG-Desktop-Token: {token}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
        );
        let _ = stream.set_write_timeout(Some(Duration::from_millis(500)));
        let _ = stream.write_all(request.as_bytes());
    }

    let deadline = Instant::now() + Duration::from_secs(2);
    while Instant::now() < deadline {
        if matches!(child.try_wait(), Ok(Some(_))) {
            return;
        }
        thread::sleep(Duration::from_millis(100));
    }

    let _ = child.kill();
    let _ = child.wait();
}

fn set_startup_error(window: &tauri::WebviewWindow, message: &str) {
    let safe_message = format!("{message}");
    let encoded = format!("{:?}", safe_message);
    let script = format!(
        "document.querySelector('[data-startup-title]').textContent = 'LightWeightRAG could not start'; document.querySelector('[data-startup-message]').textContent = {encoded}; document.querySelector('[data-startup-spinner]').hidden = true; document.querySelector('[data-startup-help]').hidden = false;"
    );
    let _ = window.eval(&script);
}

fn main() {
    tauri::Builder::default()
        .setup(|app| {
            let token_slot = Arc::new(Mutex::new(None));
            let process = BackendProcess {
                child: Arc::new(Mutex::new(None)),
                closing: Arc::new(AtomicBool::new(false)),
                token: Arc::clone(&token_slot),
            };
            let child_slot = Arc::clone(&process.child);
            let closing = Arc::clone(&process.closing);
            app.manage(process);

            let window = WebviewWindowBuilder::new(
                app,
                "main",
                WebviewUrl::App("index.html".into()),
            )
            .title("LightWeightRAG")
            .inner_size(1180.0, 820.0)
            .min_inner_size(760.0, 600.0)
            .build()?;

            let app_handle = app.handle().clone();
            let startup_window = window.clone();
            thread::spawn(move || {
                let token = uuid::Uuid::new_v4().to_string();
                if let Ok(mut slot) = token_slot.lock() {
                    *slot = Some(token.clone());
                }
                let mut child = match start_backend(&app_handle, &token) {
                    Ok(child) => child,
                    Err(message) => {
                        set_startup_error(&startup_window, &message);
                        return;
                    }
                };

                if let Err(message) = wait_for_backend(&mut child, &token) {
                    let _ = child.kill();
                    set_startup_error(&startup_window, &message);
                    return;
                }

                if closing.load(Ordering::SeqCst) {
                    let _ = child.kill();
                    return;
                }
                if let Ok(mut slot) = child_slot.lock() {
                    if closing.load(Ordering::SeqCst) {
                        let _ = child.kill();
                        return;
                    }
                    *slot = Some(child);
                } else {
                    let _ = child.kill();
                    set_startup_error(&startup_window, "The local app could not finish starting. Close the app and try again.");
                    return;
                }

                match local_backend_url(BACKEND_PORT, &token) {
                    Ok(url) => {
                        if let Err(error) = startup_window.navigate(url) {
                            set_startup_error(&startup_window, &format!("The research workspace could not be opened: {error}"));
                        }
                    }
                    Err(message) => set_startup_error(&startup_window, &message),
                }
            });
            Ok(())
        })
        .on_window_event(|window, event| {
            if matches!(event, WindowEvent::CloseRequested { .. }) {
                let process = window.state::<BackendProcess>();
                process.closing.store(true, Ordering::SeqCst);
                let child = process.child.lock().ok().and_then(|mut slot| slot.take());
                if let Some(mut child) = child {
                    let token = process
                        .token
                        .lock()
                        .ok()
                        .and_then(|slot| slot.clone())
                        .unwrap_or_default();
                    stop_backend(&mut child, &token);
                }
            }
        })
        .run(tauri::generate_context!())
        .expect("LightWeightRAG desktop app failed to start");
}
