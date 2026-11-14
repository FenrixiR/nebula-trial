

use std::ffi::CString;
use std::io;
use std::net::{TcpListener, TcpStream, ToSocketAddrs};
use std::os::unix::io::{AsRawFd, RawFd};
use std::time::{Duration, Instant};

const MAX_BUF: usize = 64 * 1024;
const DEAD: i16 = libc::POLLERR | libc::POLLHUP | libc::POLLNVAL;

fn log(name: &str, msg: String) {
    eprintln!("[{}] {}", name, msg);
}

fn errno() -> i32 {
    io::Error::last_os_error().raw_os_error().unwrap_or(0)
}

fn pfd(fd: RawFd, events: i16) -> libc::pollfd {
    libc::pollfd { fd, events, revents: 0 }
}

fn poll(fds: &mut [libc::pollfd], ms: i32) {
    unsafe {
        libc::poll(fds.as_mut_ptr(), fds.len() as libc::nfds_t, ms);
    }
}

fn set_nonblock(fd: RawFd) {
    unsafe {
        let f = libc::fcntl(fd, libc::F_GETFL);
        libc::fcntl(fd, libc::F_SETFL, f | libc::O_NONBLOCK);
    }
}

enum Rd {
    Data(usize),
    Empty,
    Gone,
}

fn rd(fd: RawFd, buf: &mut [u8]) -> Rd {
    let n = unsafe { libc::read(fd, buf.as_mut_ptr() as *mut libc::c_void, buf.len()) };
    if n > 0 {
        return Rd::Data(n as usize);
    }
    if n == 0 {
        return Rd::Gone;
    }
    match errno() {
        libc::EAGAIN | libc::EINTR => Rd::Empty,
        _ => Rd::Gone,
    }
}

fn flush(fd: RawFd, buf: &mut Vec<u8>) -> bool {
    while !buf.is_empty() {
        let n = unsafe { libc::write(fd, buf.as_ptr() as *const libc::c_void, buf.len()) };
        if n > 0 {
            buf.drain(..n as usize);
            continue;
        }
        if n == 0 {
            return true;
        }
        return matches!(errno(), libc::EAGAIN | libc::EINTR);
    }
    true
}

fn push(buf: &mut Vec<u8>, data: &[u8]) {
    if buf.len() + data.len() <= MAX_BUF {
        buf.extend_from_slice(data);
    }
}

fn sso(fd: RawFd, level: libc::c_int, opt: libc::c_int, val: libc::c_int) {
    unsafe {
        libc::setsockopt(
            fd,
            level,
            opt,
            &val as *const libc::c_int as *const libc::c_void,
            std::mem::size_of::<libc::c_int>() as libc::socklen_t,
        );
    }
}

fn tune(s: &TcpStream) {
    let fd = s.as_raw_fd();
    let _ = s.set_nodelay(true);
    sso(fd, libc::SOL_SOCKET, libc::SO_KEEPALIVE, 1);
    sso(fd, libc::IPPROTO_TCP, libc::TCP_KEEPIDLE, 2);
    sso(fd, libc::IPPROTO_TCP, libc::TCP_KEEPINTVL, 1);
    sso(fd, libc::IPPROTO_TCP, libc::TCP_KEEPCNT, 4);
    sso(fd, libc::IPPROTO_TCP, libc::TCP_USER_TIMEOUT, 8000);
    set_nonblock(fd);
}

#[repr(C)]
struct Termios2 {
    c_iflag: u32,
    c_oflag: u32,
    c_cflag: u32,
    c_lflag: u32,
    c_line: u8,
    c_cc: [u8; 19],
    c_ispeed: u32,
    c_ospeed: u32,
}
const TCGETS2: libc::c_ulong = 0x802c542a;
const TCSETS2: libc::c_ulong = 0x402c542b;
const BOTHER: u32 = 0o010000;

fn set_raw(fd: RawFd, baud: u32) {
    unsafe {
        let mut t: Termios2 = std::mem::zeroed();
        if libc::ioctl(fd, TCGETS2 as _, &mut t as *mut Termios2) != 0 {
            return;
        }
        t.c_iflag = 0;
        t.c_oflag = 0;
        t.c_lflag = 0;
        t.c_cflag = (libc::CS8 | libc::CREAD | libc::CLOCAL) as u32 | BOTHER;
        t.c_cc[libc::VMIN] = 1;
        t.c_cc[libc::VTIME] = 0;
        t.c_ispeed = baud;
        t.c_ospeed = baud;
        libc::ioctl(fd, TCSETS2 as _, &t as *const Termios2);
    }
}

fn open_dev(path: &str, baud: u32) -> Result<RawFd, i32> {
    let c = CString::new(path).map_err(|_| libc::EINVAL)?;
    let fd = unsafe { libc::open(c.as_ptr(), libc::O_RDWR | libc::O_NOCTTY | libc::O_NONBLOCK) };
    if fd < 0 {
        return Err(errno());
    }
    if unsafe { libc::isatty(fd) } == 1 {
        set_raw(fd, baud);
    }
    Ok(fd)
}

fn close(fd: RawFd) {
    unsafe {
        libc::close(fd);
    }
}

struct ServeSpec {
    name: String,
    dev: String,
    baud: u32,
    port: u16,
}

fn serve_link(sp: ServeSpec, bind: String, allow: Vec<String>) {
    let name = sp.name.clone();
    let listener = match TcpListener::bind((bind.as_str(), sp.port)) {
        Ok(l) => l,
        Err(e) => {
            log(&name, format!("cannot listen on {}:{}: {}", bind, sp.port, e));
            std::process::exit(1);
        }
    };
    listener.set_nonblocking(true).ok();
    log(&name, format!("listening on {}:{} for {} @ {}", bind, sp.port, sp.dev, sp.baud));

    let mut client: Option<TcpStream> = None;
    let mut dev: Option<RawFd> = None;
    let mut c2d: Vec<u8> = Vec::new();
    let mut d2c: Vec<u8> = Vec::new();
    let mut next_open = Instant::now();
    let mut warned = false;
    let mut rb = [0u8; 4096];

    loop {
        if client.is_some() && dev.is_none() && Instant::now() >= next_open {
            match open_dev(&sp.dev, sp.baud) {
                Ok(fd) => {
                    log(&name, format!("opened {}", sp.dev));
                    dev = Some(fd);
                    warned = false;
                }
                Err(e) => {
                    if !warned {
                        log(&name, format!("cannot open {} (errno {}), retrying", sp.dev, e));
                        warned = true;
                    }
                    next_open = Instant::now() + Duration::from_millis(200);
                }
            }
        }

        let mut fds = vec![pfd(listener.as_raw_fd(), libc::POLLIN)];
        let ci = client.as_ref().map(|c| {
            let ev = libc::POLLIN | if d2c.is_empty() { 0 } else { libc::POLLOUT };
            fds.push(pfd(c.as_raw_fd(), ev));
            fds.len() - 1
        });
        let di = dev.map(|fd| {
            let ev = libc::POLLIN | if c2d.is_empty() { 0 } else { libc::POLLOUT };
            fds.push(pfd(fd, ev));
            fds.len() - 1
        });
        poll(&mut fds, 100);

        if fds[0].revents & libc::POLLIN != 0 {
            if let Ok((s, addr)) = listener.accept() {
                if !allow.is_empty() && !allow.contains(&addr.ip().to_string()) {
                    log(&name, format!("refused client {} (only {} allowed)", addr, allow.join(",")));
                    continue;
                }
                log(&name, format!("client {}", addr));
                tune(&s);
                if let Some(fd) = dev.take() {
                    close(fd);
                }
                client = Some(s);
                c2d.clear();
                d2c.clear();
                next_open = Instant::now();
                continue;
            }
        }

        let mut drop_client = false;
        if let (Some(i), Some(c)) = (ci, client.as_ref()) {
            let rev = fds[i].revents;
            if rev & libc::POLLIN != 0 {
                match rd(c.as_raw_fd(), &mut rb) {
                    Rd::Data(n) => {
                        if dev.is_some() {
                            push(&mut c2d, &rb[..n]);
                        }
                    }
                    Rd::Empty => {}
                    Rd::Gone => drop_client = true,
                }
            } else if rev & DEAD != 0 {
                drop_client = true;
            }
            if !drop_client && !flush(c.as_raw_fd(), &mut d2c) {
                drop_client = true;
            }
        }
        if drop_client {
            log(&name, "client gone".to_string());
            client = None;
            if let Some(fd) = dev.take() {
                close(fd);
            }
            c2d.clear();
            d2c.clear();
            continue;
        }

        let mut dev_gone = false;
        if let (Some(i), Some(fd)) = (di, dev) {
            let rev = fds[i].revents;
            if rev & libc::POLLIN != 0 {
                match rd(fd, &mut rb) {
                    Rd::Data(n) => push(&mut d2c, &rb[..n]),
                    Rd::Empty => {}
                    Rd::Gone => dev_gone = true,
                }
            } else if rev & DEAD != 0 {
                dev_gone = true;
            }
            if !dev_gone && !flush(fd, &mut c2d) {
                dev_gone = true;
            }
        }
        if dev_gone {
            if let Some(fd) = dev.take() {
                close(fd);
            }
            log(&name, "device went away, reopening".to_string());
            next_open = Instant::now() + Duration::from_millis(100);
            c2d.clear();
        }
    }
}

struct PtySpec {
    name: String,
    remote: String,
    link: String,
}

fn make_pty(name: &str, link: &str) -> (RawFd, RawFd) {
    unsafe {
        let m = libc::posix_openpt(libc::O_RDWR | libc::O_NOCTTY);
        if m < 0 || libc::grantpt(m) != 0 || libc::unlockpt(m) != 0 {
            log(name, format!("cannot create pty (errno {})", errno()));
            std::process::exit(1);
        }
        let mut path = [0 as libc::c_char; 128];
        libc::ptsname_r(m, path.as_mut_ptr(), path.len());
        let slave = std::ffi::CStr::from_ptr(path.as_ptr()).to_string_lossy().into_owned();

        let c = CString::new(slave.clone()).unwrap();
        let s = libc::open(c.as_ptr(), libc::O_RDWR | libc::O_NOCTTY | libc::O_NONBLOCK);
        set_raw(m, 115200);
        let _ = std::fs::remove_file(link);
        if let Err(e) = std::os::unix::fs::symlink(&slave, link) {
            log(name, format!("cannot create {}: {}", link, e));
            std::process::exit(1);
        }
        set_nonblock(m);
        log(name, format!("{} -> {}", link, slave));
        (m, s)
    }
}

fn pty_link(sp: PtySpec) {
    let name = sp.name.clone();
    let (master, _slave) = make_pty(&name, &sp.link);
    let mut rb = [0u8; 4096];
    let mut was_up = false;

    loop {

        let stream = sp.remote.split(',').find_map(|r| {
            let a = r.to_socket_addrs().ok().and_then(|mut a| a.next())?;
            TcpStream::connect_timeout(&a, Duration::from_millis(500)).ok().map(|s| (s, r))
        });
        let (s, remote) = match stream {
            Some(x) => x,
            None => {

                let mut fds = [pfd(master, libc::POLLIN)];
                poll(&mut fds, 300);
                while let Rd::Data(_) = rd(master, &mut rb) {}
                continue;
            }
        };
        tune(&s);
        log(&name, format!("{}connected to {}", if was_up { "re" } else { "" }, remote));
        was_up = true;
        let mut tx: Vec<u8> = Vec::new();
        let mut rx: Vec<u8> = Vec::new();

        loop {
            let mut fds = [
                pfd(master, libc::POLLIN | if rx.is_empty() { 0 } else { libc::POLLOUT }),
                pfd(s.as_raw_fd(), libc::POLLIN | if tx.is_empty() { 0 } else { libc::POLLOUT }),
            ];
            poll(&mut fds, 1000);
            if fds[0].revents & libc::POLLIN != 0 {
                if let Rd::Data(n) = rd(master, &mut rb) {
                    push(&mut tx, &rb[..n]);
                }
            }
            let mut dead = false;
            if fds[1].revents & libc::POLLIN != 0 {
                match rd(s.as_raw_fd(), &mut rb) {
                    Rd::Data(n) => push(&mut rx, &rb[..n]),
                    Rd::Empty => {}
                    Rd::Gone => dead = true,
                }
            } else if fds[1].revents & DEAD != 0 {
                dead = true;
            }
            if !dead && !flush(s.as_raw_fd(), &mut tx) {
                dead = true;
            }
            let _ = flush(master, &mut rx);
            if dead {
                log(&name, "link down, reconnecting".to_string());
                break;
            }
        }
    }
}

fn usage() -> ! {
    eprintln!(
        "usage:\n  centauri-link serve [--bind ADDR] [--allow PEER_IP]... NAME=DEVICE[:BAUD]@PORT ...\n  centauri-link pty NAME=HOST:PORT[,HOST:PORT...]@LINKPATH ..."
    );
    std::process::exit(2);
}

fn parse_serve(spec: &str) -> Option<ServeSpec> {
    let (left, port) = spec.rsplit_once('@')?;
    let (name, devspec) = left.split_once('=')?;
    let (dev, baud) = match devspec.rsplit_once(':') {
        Some((d, b)) if b.parse::<u32>().is_ok() => (d, b.parse().unwrap()),
        _ => (devspec, 115200),
    };
    Some(ServeSpec { name: name.into(), dev: dev.into(), baud, port: port.parse().ok()? })
}

fn parse_pty(spec: &str) -> Option<PtySpec> {
    let (name, rest) = spec.split_once('=')?;
    let (remote, link) = rest.split_once('@')?;
    Some(PtySpec { name: name.into(), remote: remote.into(), link: link.into() })
}

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let mut threads = Vec::new();
    let spawn = |f: Box<dyn FnOnce() + Send>| {
        std::thread::Builder::new().stack_size(256 * 1024).spawn(f).expect("thread")
    };
    match args.first().map(String::as_str) {
        Some("--version") => println!("centauri-link {}", env!("CARGO_PKG_VERSION")),
        Some("serve") => {
            let mut bind = "0.0.0.0".to_string();
            let mut allow: Vec<String> = Vec::new();
            let mut rest = &args[1..];
            loop {
                match rest.first().map(String::as_str) {
                    Some("--bind") => {
                        bind = rest.get(1).cloned().unwrap_or_else(|| usage());
                        rest = &rest[2..];
                    }
                    Some("--allow") => {
                        allow.push(rest.get(1).cloned().unwrap_or_else(|| usage()));
                        rest = &rest[2..];
                    }
                    _ => break,
                }
            }
            if rest.is_empty() {
                usage();
            }
            for a in rest {
                let sp = parse_serve(a).unwrap_or_else(|| usage());
                let b = bind.clone();
                let al = allow.clone();
                threads.push(spawn(Box::new(move || serve_link(sp, b, al))));
            }
        }
        Some("pty") => {
            if args.len() < 2 {
                usage();
            }
            for a in &args[1..] {
                let sp = parse_pty(a).unwrap_or_else(|| usage());
                threads.push(spawn(Box::new(move || pty_link(sp))));
            }
        }
        _ => usage(),
    }
    for t in threads {
        let _ = t.join();
    }
}
