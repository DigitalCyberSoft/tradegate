/*
 * twsquiet — LD_PRELOAD shim that stops TWS (Java/AWT) from stealing focus.
 *
 * Injected by tradegate into the TWS process only. Three interventions:
 *
 *   1. XSetInputFocus: AWT grabs focus by calling XSetInputFocus on its
 *      invisible focus proxy (jdk17u XlibWrapper.c:409, proxy class set in
 *      XFocusProxyWindow.java:58). The call uses either CurrentTime or a
 *      fresh server timestamp the JDK fetches itself, so the timestamp does
 *      NOT prove the WM sanctioned it. We drop proxy grabs unless there is
 *      evidence of user intent (see has_user_intent / the input monitor).
 *
 *   2. XSendEvent(_NET_ACTIVE_WINDOW, source=application): AWT deiconify
 *      (Frame.setState(NORMAL), jdk17u XFramePeer.java:285) sends
 *      _NET_ACTIVE_WINDOW to self-activate. Same user-intent gate as the
 *      focus/raise paths (recent WM_TAKE_FOCUS AND recent physical input).
 *
 *   3. XCreateWindow/XCreateSimpleWindow: stamp _NET_WM_USER_TIME=0 on new
 *      top-level windows. Stock OpenJDK never sets the property, and Muffin
 *      grants map-time focus to windows with no user-time info but refuses
 *      it when the property is 0 (muffin window.c:2178). This kills the
 *      focus grab when the login/main window first maps.
 *
 *   4. XRaiseWindow / XMapRaised / XConfigureWindow(stack Above): how TWS
 *      "pulls to the front" on launch/reconnect. Gated on the same user-intent
 *      test; a self-initiated raise is dropped, and XMapRaised maps without
 *      raising. Off-map self-raise/refocus (reconnect, restart-while-mapped)
 *      is fully gated.
 *
 *   5. Deferred main-window lower. Muffin still places a newly-mapped normal
 *      window's frame on top even when it denies it focus (user_time=0):
 *      window_state_on_map() sets places_on_top=FALSE, but the "stack just
 *      below focus" branch (muffin window.c:2604) does not fire for us, so the
 *      window maps ON TOP yet UNFOCUSED. In-process XLowerWindow cannot fix it
 *      (the client is reparented into a WM frame, so lowering the client is a
 *      no-op on the frame order), and _NET_WM_STATE_BELOW traps the window in
 *      the bottom layer (the user can no longer raise it). Instead the monitor
 *      thread, once the main window is mapped and titled, sends one
 *      _NET_RESTACK_WINDOW (detail=Below) to drop the frame to the bottom of
 *      the NORMAL layer. Every self-raise is already blocked, so the lower
 *      sticks; the window stays a normal window the user can raise. Scoped to
 *      the main window by title so login/dialogs and user-opened (recent-input)
 *      windows are untouched.
 *
 * Modes (TWSQUIET_MODE): "log" observes and logs decisions without blocking;
 * "enforce" blocks. Everything else disables the policy (interposers pass
 * straight through). TWSQUIET_LOG sets the log path (default
 * $XDG_RUNTIME_DIR/twsquiet.log, else /tmp/twsquiet.log).
 * TWSQUIET_IDLE_MS sets the recent-input threshold (default 3000).
 *
 * Prior art: joka90/matlab-focus-fixer (same AWT proxy, endorsed in muffin
 * commit ee9a615) and CyberShadow/hax11 (_NET_ACTIVE_WINDOW filtering).
 */

#define _GNU_SOURCE
#include <dlfcn.h>
#include <fcntl.h>
#include <pthread.h>
#include <stdarg.h>
#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/select.h>
#include <time.h>
#include <unistd.h>

#include <X11/Xatom.h>
#include <X11/Xlib.h>
#include <X11/Xutil.h>
#include <X11/keysym.h>
#include <X11/extensions/XInput2.h>

#define MODE_OFF 0
#define MODE_LOG 1
#define MODE_ENFORCE 2

/* A WM_TAKE_FOCUS response follows the ClientMessage within one event-loop
 * dispatch; 500ms is generous slack against a busy AWT EDT. Used only in the
 * degraded fallback when the global input monitor could not start. */
#define WM_TAKE_FOCUS_WINDOW_MS 500

/* How recent a real click / window-switch key must be to count as the user
 * asking for the focus/raise. Covers the gap between the gesture and the
 * WM's resulting WM_TAKE_FOCUS. */
#define INTENT_WINDOW_MS 1500

static int g_mode = MODE_OFF;
static long g_idle_ms = 3000;
static const char *g_log_path = NULL;
static int g_log_fd = -1;
static atomic_bool g_banner_done = false;
static atomic_long g_last_input_ms = 0;  /* CLOCK_MONOTONIC ms of last key/button event; 0 = never */
static pthread_mutex_t g_lock = PTHREAD_MUTEX_INITIALIZER;

/* Real functions, resolved lazily. */
static int (*real_XSetInputFocus)(Display *, Window, int, Time);
static Status (*real_XSendEvent)(Display *, Window, Bool, long, XEvent *);
static Window (*real_XCreateWindow)(Display *, Window, int, int, unsigned int,
                                    unsigned int, unsigned int, int, unsigned int,
                                    Visual *, unsigned long, XSetWindowAttributes *);
static Window (*real_XCreateSimpleWindow)(Display *, Window, int, int, unsigned int,
                                          unsigned int, unsigned int, unsigned long,
                                          unsigned long);
static int (*real_XRaiseWindow)(Display *, Window);
static int (*real_XMapWindow)(Display *, Window);
static int (*real_XMapRaised)(Display *, Window);
static int (*real_XConfigureWindow)(Display *, Window, unsigned int, XWindowChanges *);
static int (*real_XNextEvent)(Display *, XEvent *);
static int (*real_XIfEvent)(Display *, XEvent *,
                            Bool (*)(Display *, XEvent *, XPointer), XPointer);
static Bool (*real_XCheckIfEvent)(Display *, XEvent *,
                                  Bool (*)(Display *, XEvent *, XPointer), XPointer);

static long now_ms(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return ts.tv_sec * 1000L + ts.tv_nsec / 1000000L;
}

static void *must_sym(const char *name) {
    void *fn = dlsym(RTLD_NEXT, name);
    /* Cannot happen in a process that reached this interposer (the caller
     * linked libX11), but fail loud rather than crash on a null call. */
    if (!fn) {
        const char *msg = "twsquiet: FATAL dlsym failed, passing through impossible\n";
        ssize_t r = write(STDERR_FILENO, msg, strlen(msg));
        (void)r;
    }
    return fn;
}

__attribute__((constructor)) static void twsquiet_init(void) {
    const char *mode = getenv("TWSQUIET_MODE");
    if (mode && strcmp(mode, "log") == 0)
        g_mode = MODE_LOG;
    else if (mode && strcmp(mode, "enforce") == 0)
        g_mode = MODE_ENFORCE;

    const char *idle = getenv("TWSQUIET_IDLE_MS");
    if (idle && atol(idle) > 0)
        g_idle_ms = atol(idle);

    g_log_path = getenv("TWSQUIET_LOG");
    /* No X calls and no file I/O here: this constructor also runs inside
     * every subprocess TWS spawns (JxBrowser chromium). Logging starts on
     * first interposed call, which those processes never make via Xlib. */
}

static void logmsg(const char *fmt, ...) {
    if (g_mode == MODE_OFF)
        return;
    pthread_mutex_lock(&g_lock);
    if (g_log_fd < 0) {
        const char *path = g_log_path;
        char fallback[256];
        if (!path || !*path) {
            const char *rt = getenv("XDG_RUNTIME_DIR");
            snprintf(fallback, sizeof fallback, "%s/twsquiet.log", rt && *rt ? rt : "/tmp");
            path = fallback;
        }
        g_log_fd = open(path, O_WRONLY | O_CREAT | O_APPEND, 0600);
        if (g_log_fd < 0)
            g_log_fd = STDERR_FILENO;
    }
    if (!atomic_exchange(&g_banner_done, true)) {
        char comm[64] = "?";
        FILE *f = fopen("/proc/self/comm", "r");
        if (f) {
            if (fgets(comm, sizeof comm, f))
                comm[strcspn(comm, "\n")] = 0;
            fclose(f);
        }
        char banner[256];
        int n = snprintf(banner, sizeof banner,
                         "[twsquiet pid=%d comm=%s] loaded mode=%s idle_ms=%ld\n",
                         getpid(), comm, g_mode == MODE_ENFORCE ? "enforce" : "log",
                         g_idle_ms);
        ssize_t r = write(g_log_fd, banner, (size_t)n);
        (void)r;
    }
    char buf[512];
    int n = snprintf(buf, sizeof buf, "[twsquiet pid=%d t=%ldms] ", getpid(), now_ms());
    va_list ap;
    va_start(ap, fmt);
    n += vsnprintf(buf + n, sizeof buf - (size_t)n, fmt, ap);
    va_end(ap);
    if (n > (int)sizeof buf - 2)
        n = (int)sizeof buf - 2;
    buf[n++] = '\n';
    ssize_t r = write(g_log_fd, buf, (size_t)n);
    (void)r;
    pthread_mutex_unlock(&g_lock);
}

/* Age of last user input seen by THIS process, in ms. LONG_MAX-ish if never. */
#define AGE_NEVER (1L << 40)
static long input_age_ms(void) {
    long last = atomic_load(&g_last_input_ms);
    if (last == 0)
        return AGE_NEVER;
    return now_ms() - last;
}

/* Format an input age for logging: "never" beats a 2^40 sentinel in a log. */
static const char *age_str(long age, char *buf, size_t len) {
    if (age >= AGE_NEVER)
        return "never";
    snprintf(buf, len, "%ld", age);
    return buf;
}

/* CLOCK_MONOTONIC ms of the last WM_TAKE_FOCUS ClientMessage TWS received.
 * A proxy focus grab is legitimate (user clicked the window/decoration) only
 * when it responds to one of these; a self-steal has none behind it. */
static atomic_long g_last_take_focus_ms = 0;

static void track_event(const XEvent *ev) {
    if (!ev)
        return;
    switch (ev->type) {
    case KeyPress:
    case KeyRelease:
    case ButtonPress:
    case ButtonRelease:
        atomic_store(&g_last_input_ms, now_ms());
        break;
    case ClientMessage: {
        /* Detect WM_TAKE_FOCUS: ClientMessage whose type is WM_PROTOCOLS
         * and whose first data word is the WM_TAKE_FOCUS atom. Atoms are
         * interned lazily off the event's own display. */
        static atomic_long wm_protocols = 0, wm_take_focus = 0;
        Display *dpy = ev->xany.display;
        if (dpy && atomic_load(&wm_protocols) == 0) {
            atomic_store(&wm_protocols, (long)XInternAtom(dpy, "WM_PROTOCOLS", False));
            atomic_store(&wm_take_focus, (long)XInternAtom(dpy, "WM_TAKE_FOCUS", False));
        }
        if ((long)ev->xclient.message_type == atomic_load(&wm_protocols) &&
            ev->xclient.format == 32 &&
            ev->xclient.data.l[0] == atomic_load(&wm_take_focus)) {
            atomic_store(&g_last_take_focus_ms, now_ms());
        }
        break;
    }
    }
}

/* Age in ms of the last WM_TAKE_FOCUS; AGE_NEVER if none seen. */
static long take_focus_age_ms(void) {
    long last = atomic_load(&g_last_take_focus_ms);
    if (last == 0)
        return AGE_NEVER;
    return now_ms() - last;
}

/* ------------------------------------------------------------------ */
/* Global input monitor                                                */
/*                                                                     */
/* The in-process signals above cannot tell a user alt-tab / title-bar */
/* click (both legitimate) from WM focus succession when a focused     */
/* window closes (a takeover): all three reach TWS as an identical     */
/* WM_TAKE_FOCUS with no app-side input. The discriminator is whether  */
/* a real click or window-switch key happened just before. A monitor   */
/* thread on its OWN X connection watches XInput2 raw events for that.  */
/* Plain typing is deliberately NOT tracked: typing in another window  */
/* is not a request to focus TWS.                                      */
/* ------------------------------------------------------------------ */

static atomic_long g_last_click_ms = 0;
static atomic_long g_last_switch_ms = 0;
static atomic_int g_monitor_state = 0;  /* 0=starting, 1=active, 2=failed */
static pthread_once_t g_monitor_once = PTHREAD_ONCE_INIT;

/* ------------------------------------------------------------------ */
/* Deferred main-window lower (intervention 5 in the file header)      */
/* ------------------------------------------------------------------ */

#define LOWER_QUEUE 64
#define LOWER_TITLE_WAIT_MS 8000   /* give up matching a title after this */
typedef struct { Window win; long enq_ms; } lower_entry;
static lower_entry g_lower_q[LOWER_QUEUE];
static int g_lower_count = 0;
static pthread_mutex_t g_lower_lock = PTHREAD_MUTEX_INITIALIZER;
static int g_wake_pipe[2] = { -1, -1 };

/* Main-window "pin": TWS re-raises the main window during startup (it maximizes
 * it, and Muffin raises on the _NET_WM_STATE change; blocking every raise vector
 * is whack-a-mole). So after the initial lower we KEEP the window at the bottom
 * for a bounded startup window, re-lowering it whenever it climbs, until the
 * user brings it forward (intent) or the window elapses. Monitor thread only. */
#define PIN_MS 20000
static Window g_pin_win = 0;
static long g_pin_until_ms = 0;
static long g_last_relower_ms = 0;

/* Defined below with the rest of the intent machinery; needed by process_pin(). */
static int has_user_intent(Display *dpy, char *detail, size_t dlen, const char **why);

/* Swallow async X errors on the monitor's OWN connection (e.g. XFetchName on a
 * window that closed during the wait); chain everything else to whatever
 * handler AWT installed. A stray BadWindow reaching Xlib's default handler
 * would call exit() and kill TWS, so this containment is load-bearing. */
static Display *g_mon_dpy = NULL;
static XErrorHandler g_prev_xerror = NULL;
static int mon_error_handler(Display *d, XErrorEvent *e) {
    if (d == g_mon_dpy)
        return 0;
    if (g_prev_xerror)
        return g_prev_xerror(d, e);
    return 0;
}

static void wake_monitor(void) {
    if (g_wake_pipe[1] >= 0) {
        char b = 1;
        ssize_t r = write(g_wake_pipe[1], &b, 1);
        (void)r;
    }
}

/* 1 = title contains the main-window marker; 0 = no title yet OR a different
 * title. TWS sets/changes WM_NAME AFTER map and may show a placeholder first,
 * so the caller keeps polling until this matches or times out; it MUST NOT
 * give up on a single non-matching title. */
static int title_matches_main(Display *mon, Window w) {
    char *name = NULL;
    if (!XFetchName(mon, w, &name) || !name)
        return 0;
    int match = strcasestr(name, "interactive brokers") != NULL;
    XFree(name);
    return match;
}

static void restack_below(Display *mon, Window w) {
    static Atom net_restack = 0;
    if (!net_restack)
        net_restack = XInternAtom(mon, "_NET_RESTACK_WINDOW", False);
    XEvent e = {0};
    e.xclient.type = ClientMessage;
    e.xclient.window = w;
    e.xclient.message_type = net_restack;
    e.xclient.format = 32;
    e.xclient.data.l[0] = 2;   /* source indication: pager */
    e.xclient.data.l[1] = 0;   /* sibling: None -> bottom of the layer */
    e.xclient.data.l[2] = 1;   /* detail: Below */
    XSendEvent(mon, DefaultRootWindow(mon), False,
               SubstructureRedirectMask | SubstructureNotifyMask, &e);
    XFlush(mon);
}

/* True if w is the bottom entry of _NET_CLIENT_LIST_STACKING (reads first item
 * only). On this WM the normal layer reaches index 0, so this means our lower
 * took effect and re-lowering would be a no-op. */
static int window_is_lowest(Display *mon, Window w) {
    static Atom prop = 0;
    if (!prop)
        prop = XInternAtom(mon, "_NET_CLIENT_LIST_STACKING", False);
    Atom type; int fmt; unsigned long nitems, after; unsigned char *data = NULL;
    int rc = XGetWindowProperty(mon, DefaultRootWindow(mon), prop, 0, 1, False, XA_WINDOW,
                                &type, &fmt, &nitems, &after, &data);
    int lowest = (rc == Success && data && nitems >= 1 && ((Window *)data)[0] == w);
    if (data)
        XFree(data);
    return lowest;
}

static void lower_q_remove(Window w) {
    pthread_mutex_lock(&g_lower_lock);
    for (int i = 0; i < g_lower_count; i++) {
        if (g_lower_q[i].win == w) {
            g_lower_q[i] = g_lower_q[--g_lower_count];
            break;
        }
    }
    pthread_mutex_unlock(&g_lower_lock);
}

/* Runs on the monitor thread only; safe to call X on `mon`. */
static void process_pending_lowers(Display *mon) {
    lower_entry snap[LOWER_QUEUE];
    int n;
    pthread_mutex_lock(&g_lower_lock);
    n = g_lower_count;
    for (int i = 0; i < n; i++)
        snap[i] = g_lower_q[i];
    pthread_mutex_unlock(&g_lower_lock);

    for (int i = 0; i < n; i++) {
        Window w = snap[i].win;
        if (title_matches_main(mon, w)) {
            if (g_mode == MODE_ENFORCE) {
                restack_below(mon, w);
                g_pin_win = w;
                g_pin_until_ms = now_ms() + PIN_MS;
                g_last_relower_ms = now_ms();
                logmsg("main window 0x%lx mapped → _NET_RESTACK_WINDOW below "
                       "(open in back); pinned %dms", (unsigned long)w, PIN_MS);
            } else {
                logmsg("main window 0x%lx mapped → would restack below (log mode)",
                       (unsigned long)w);
            }
            lower_q_remove(w);
        } else if (now_ms() - snap[i].enq_ms > LOWER_TITLE_WAIT_MS) {
            /* Never showed the main-window title within the window: a login
             * screen, dialog, or other top-level. Leave it where it is. */
            logmsg("window 0x%lx: no main-window title after %dms → not lowering",
                   (unsigned long)w, LOWER_TITLE_WAIT_MS);
            lower_q_remove(w);
        }
        /* else: keep polling; the title may still change to the main one */
    }
}

/* Keep the pinned main window at the bottom during startup. Monitor thread only. */
static void process_pin(Display *mon) {
    if (!g_pin_win)
        return;
    if (now_ms() >= g_pin_until_ms) {
        logmsg("main window 0x%lx unpinned (startup window elapsed)",
               (unsigned long)g_pin_win);
        g_pin_win = 0;
        return;
    }
    char detail[96];
    const char *why;
    if (has_user_intent(mon, detail, sizeof detail, &why)) {
        logmsg("main window 0x%lx unpinned (user brought it forward: %s)",
               (unsigned long)g_pin_win, why);
        g_pin_win = 0;
        return;
    }
    if (now_ms() - g_last_relower_ms < 20)   /* rate-limit; also breaks any ping-pong */
        return;
    if (!window_is_lowest(mon, g_pin_win)) {
        restack_below(mon, g_pin_win);
        g_last_relower_ms = now_ms();
        logmsg("main window 0x%lx re-lowered (raised during startup, no user intent)",
               (unsigned long)g_pin_win);
    }
}


static void *monitor_thread(void *arg) {
    (void)arg;
    Display *dpy = XOpenDisplay(NULL);
    if (!dpy) {
        atomic_store(&g_monitor_state, 2);
        logmsg("input monitor: XOpenDisplay failed; degraded to WM_TAKE_FOCUS gate");
        return NULL;
    }
    g_mon_dpy = dpy;
    g_prev_xerror = XSetErrorHandler(mon_error_handler);
    int xi_op, xi_ev, xi_err;
    if (!XQueryExtension(dpy, "XInputExtension", &xi_op, &xi_ev, &xi_err)) {
        atomic_store(&g_monitor_state, 2);
        logmsg("input monitor: no XInputExtension; degraded");
        XCloseDisplay(dpy);
        return NULL;
    }
    int major = 2, minor = 0;
    if (XIQueryVersion(dpy, &major, &minor) != Success) {
        atomic_store(&g_monitor_state, 2);
        logmsg("input monitor: XI2 unavailable; degraded");
        XCloseDisplay(dpy);
        return NULL;
    }

    Window root = DefaultRootWindow(dpy);
    unsigned char mask[XIMaskLen(XI_LASTEVENT)] = {0};
    XISetMask(mask, XI_RawButtonPress);
    XISetMask(mask, XI_RawKeyPress);
    XIEventMask em = { XIAllMasterDevices, sizeof(mask), mask };
    XISelectEvents(dpy, root, &em, 1);
    /* Also watch root property changes so a climb of the pinned window is
     * caught the instant Muffin restacks (via _NET_CLIENT_LIST_STACKING),
     * rather than up to one poll interval later; this removes the map/maximize
     * flash. */
    XSelectInput(dpy, root, PropertyChangeMask);
    Atom net_stack_atom = XInternAtom(dpy, "_NET_CLIENT_LIST_STACKING", False);
    XSync(dpy, False);

    KeyCode k_alt_l = XKeysymToKeycode(dpy, XK_Alt_L);
    KeyCode k_alt_r = XKeysymToKeycode(dpy, XK_Alt_R);
    KeyCode k_super_l = XKeysymToKeycode(dpy, XK_Super_L);
    KeyCode k_super_r = XKeysymToKeycode(dpy, XK_Super_R);
    KeyCode k_meta_l = XKeysymToKeycode(dpy, XK_Meta_L);
    KeyCode k_meta_r = XKeysymToKeycode(dpy, XK_Meta_R);
    KeyCode k_tab = XKeysymToKeycode(dpy, XK_Tab);
    KeyCode k_isotab = XKeysymToKeycode(dpy, XK_ISO_Left_Tab);

    atomic_store(&g_monitor_state, 1);
    logmsg("input monitor: active (XI2 %d.%d)", major, minor);

    /* Self-pipe so an enqueued lower wakes select() even when no X event is
     * coming (unattended reconnect/restart: the user is not touching input). */
    if (pipe(g_wake_pipe) == 0) {
        fcntl(g_wake_pipe[0], F_SETFL, O_NONBLOCK);
        fcntl(g_wake_pipe[1], F_SETFL, O_NONBLOCK);
    }
    int xfd = ConnectionNumber(dpy);
    int pfd = g_wake_pipe[0];

    for (;;) {
        while (XPending(dpy)) {
            XEvent xev;
            XNextEvent(dpy, &xev);
            if (xev.type == PropertyNotify) {
                /* Pinned window may have just been restacked by the WM. */
                if (xev.xproperty.atom == net_stack_atom)
                    process_pin(dpy);
                continue;
            }
            if (xev.xcookie.type != GenericEvent || xev.xcookie.extension != xi_op)
                continue;
            if (!XGetEventData(dpy, &xev.xcookie))
                continue;
            int et = xev.xcookie.evtype;
            if (et == XI_RawButtonPress) {
                atomic_store(&g_last_click_ms, now_ms());
            } else if (et == XI_RawKeyPress) {
                XIRawEvent *re = xev.xcookie.data;
                int kc = re->detail;
                if (kc == k_alt_l || kc == k_alt_r || kc == k_super_l ||
                    kc == k_super_r || kc == k_meta_l || kc == k_meta_r ||
                    kc == k_tab || kc == k_isotab)
                    atomic_store(&g_last_switch_ms, now_ms());
            }
            XFreeEventData(dpy, &xev.xcookie);
        }

        process_pending_lowers(dpy);
        process_pin(dpy);

        pthread_mutex_lock(&g_lower_lock);
        int pending = g_lower_count;
        pthread_mutex_unlock(&g_lower_lock);
        pending = pending || (g_pin_win != 0);

        fd_set rfds;
        FD_ZERO(&rfds);
        FD_SET(xfd, &rfds);
        int maxfd = xfd;
        if (pfd >= 0) {
            FD_SET(pfd, &rfds);
            if (pfd > maxfd)
                maxfd = pfd;
        }
        /* While a lower is pending, re-poll the title every 150ms; otherwise
         * block until an X event or a wake byte arrives (no busy loop). */
        struct timeval tv = { 0, 150000 };
        select(maxfd + 1, &rfds, NULL, NULL, pending ? &tv : NULL);
        if (pfd >= 0 && FD_ISSET(pfd, &rfds)) {
            char buf[64];
            while (read(pfd, buf, sizeof buf) > 0) { }
        }
    }
    return NULL;  /* not reached */
}

static void start_monitor_once(void) {
    pthread_t t;
    if (pthread_create(&t, NULL, monitor_thread, NULL) == 0)
        pthread_detach(t);
    else
        atomic_store(&g_monitor_state, 2);
}

static void ensure_monitor(void) {
    pthread_once(&g_monitor_once, start_monitor_once);
}

/* Queue a self-mapped top-level for the deferred main-window lower. The
 * monitor thread decides by title whether it is actually the main window; a
 * login window / dialog is titled otherwise and dropped without lowering. */
static void enqueue_lower(Window w) {
    ensure_monitor();
    pthread_mutex_lock(&g_lower_lock);
    int dup = 0;
    for (int i = 0; i < g_lower_count; i++)
        if (g_lower_q[i].win == w) { dup = 1; break; }
    if (!dup && g_lower_count < LOWER_QUEUE) {
        g_lower_q[g_lower_count].win = w;
        g_lower_q[g_lower_count].enq_ms = now_ms();
        g_lower_count++;
    }
    pthread_mutex_unlock(&g_lower_lock);
    wake_monitor();
}

static long age_of(atomic_long *stamp) {
    long last = atomic_load(stamp);
    if (last == 0)
        return AGE_NEVER;
    return now_ms() - last;
}

/* True when a focus/raise the app attempts was the user bringing TWS forward.
 * TWO conditions must hold:
 *   (1) recent real input — a global click or window-switch key within
 *       INTENT_WINDOW_MS (NOT plain typing). The monitor supplies this; if it
 *       did not start, fall back to the in-process key/button signal.
 *   (2) a recent WM_TAKE_FOCUS — the WM asked TWS to take focus (because the
 *       user clicked its title bar / taskbar entry or alt-tabbed to it). A
 *       self-front on reconnect calls XSetInputFocus directly with no
 *       WM_TAKE_FOCUS behind it, so it fails (2) even if the user happens to
 *       be clicking elsewhere. Using WM_TAKE_FOCUS rather than
 *       _NET_ACTIVE_WINDOW avoids a deadlock: for a Globally-Active window,
 *       the WM sets _NET_ACTIVE_WINDOW only AFTER focus lands, so requiring it
 *       would block the very focus grab that makes it true.
 * *detail gets the numbers for the log; *why a short reason. */
static int has_user_intent(Display *dpy, char *detail, size_t dlen, const char **why) {
    ensure_monitor();
    (void)dpy;
    char a[24], b[24];
    long twf = take_focus_age_ms();
    int wm_asked = twf <= WM_TAKE_FOCUS_WINDOW_MS;
    int recent_input;
    long shown;

    if (atomic_load(&g_monitor_state) == 1) {
        long click = age_of(&g_last_click_ms);
        long sw = age_of(&g_last_switch_ms);
        shown = click < sw ? click : sw;
        recent_input = (click <= INTENT_WINDOW_MS) || (sw <= INTENT_WINDOW_MS);
        snprintf(detail, dlen, "input=%s wtf=%s",
                 age_str(shown, a, sizeof a), age_str(twf, b, sizeof b));
    } else {
        long age = input_age_ms();
        recent_input = age <= g_idle_ms;
        snprintf(detail, dlen, "appinput=%s wtf=%s [degraded]",
                 age_str(age, a, sizeof a), age_str(twf, b, sizeof b));
    }

    if (wm_asked && recent_input) { *why = "WM_TAKE_FOCUS + user input"; return 1; }
    *why = !wm_asked ? "no WM_TAKE_FOCUS (self-front)" : "no recent user input";
    return 0;
}

/* Window -> is-AWT-focus-proxy verdict cache (class hints don't change). */
#define PROXY_CACHE 32
static struct { Window win; int verdict; } g_proxy_cache[PROXY_CACHE];
static int g_proxy_next = 0;

static int is_focus_proxy(Display *dpy, Window w) {
    pthread_mutex_lock(&g_lock);
    for (int i = 0; i < PROXY_CACHE; i++) {
        if (g_proxy_cache[i].win == w) {
            int v = g_proxy_cache[i].verdict;
            pthread_mutex_unlock(&g_lock);
            return v;
        }
    }
    pthread_mutex_unlock(&g_lock);

    /* Class names per jdk17u XFocusProxyWindow.java:58. */
    int verdict = 0;
    XClassHint hint = {0};
    if (XGetClassHint(dpy, w, &hint)) {
        verdict = hint.res_name && hint.res_class &&
                  strcmp(hint.res_name, "Focus-Proxy-Window") == 0 &&
                  strcmp(hint.res_class, "FocusProxy") == 0;
        if (hint.res_name)
            XFree(hint.res_name);
        if (hint.res_class)
            XFree(hint.res_class);
    }
    pthread_mutex_lock(&g_lock);
    g_proxy_cache[g_proxy_next].win = w;
    g_proxy_cache[g_proxy_next].verdict = verdict;
    g_proxy_next = (g_proxy_next + 1) % PROXY_CACHE;
    pthread_mutex_unlock(&g_lock);
    return verdict;
}

static int is_root_window(Display *dpy, Window w) {
    for (int s = 0; s < ScreenCount(dpy); s++)
        if (w == RootWindow(dpy, s))
            return 1;
    return 0;
}

static void stamp_user_time_zero(Display *dpy, Window w) {
    static Atom atom;  /* atom ids are per-server; one TWS process = one server */
    if (!atom)
        atom = XInternAtom(dpy, "_NET_WM_USER_TIME", False);
    long zero = 0;
    XChangeProperty(dpy, w, atom, XA_CARDINAL, 32, PropModeReplace,
                    (unsigned char *)&zero, 1);
}

/* ------------------------------------------------------------------ */
/* Interposers                                                         */
/* ------------------------------------------------------------------ */

int XSetInputFocus(Display *dpy, Window focus, int revert_to, Time time) {
    if (!real_XSetInputFocus)
        real_XSetInputFocus = must_sym("XSetInputFocus");
    if (g_mode == MODE_OFF)
        return real_XSetInputFocus(dpy, focus, revert_to, time);

    /* None/PointerRoot are focus-release ops, never a takeover. */
    if (focus == None || focus == PointerRoot) {
        logmsg("XSetInputFocus win=0x%lx time=%lu → allow (release)",
               (unsigned long)focus, (unsigned long)time);
        return real_XSetInputFocus(dpy, focus, revert_to, time);
    }

    /* AWT takes focus exclusively through its invisible focus proxy
     * (jdk17u XDecoratedPeer.requestXFocus). Non-proxy focus calls are not
     * the steal path; leave them alone. */
    if (!is_focus_proxy(dpy, focus)) {
        logmsg("XSetInputFocus win=0x%lx time=%lu → allow (not focus proxy)",
               (unsigned long)focus, (unsigned long)time);
        return real_XSetInputFocus(dpy, focus, revert_to, time);
    }

    /* Proxy grab. The TIMESTAMP does NOT prove legitimacy: the JDK obtains a
     * fresh server timestamp via getCurrentServerTime() and calls
     * XSetInputFocus with it to self-activate on reconnect/restart, which is
     * exactly the takeover. Gate on user intent instead. */
    char detail[96];
    const char *why;
    int intent = has_user_intent(dpy, detail, sizeof detail, &why);

    if (intent) {
        logmsg("XSetInputFocus proxy=0x%lx time=%lu %s → allow (%s)",
               (unsigned long)focus, (unsigned long)time, detail, why);
        return real_XSetInputFocus(dpy, focus, revert_to, time);
    }

    if (g_mode == MODE_ENFORCE) {
        logmsg("XSetInputFocus proxy=0x%lx time=%lu %s → BLOCK (self-grab)",
               (unsigned long)focus, (unsigned long)time, detail);
        /* Report success so AWT's focus machinery proceeds as if the grab
         * landed. The X server never sees it. */
        return 1;
    }
    logmsg("XSetInputFocus proxy=0x%lx time=%lu %s → would BLOCK (log mode)",
           (unsigned long)focus, (unsigned long)time, detail);
    return real_XSetInputFocus(dpy, focus, revert_to, time);
}

Status XSendEvent(Display *dpy, Window w, Bool propagate, long event_mask, XEvent *ev) {
    if (!real_XSendEvent)
        real_XSendEvent = must_sym("XSendEvent");
    if (g_mode == MODE_OFF || !ev || ev->type != ClientMessage)
        return real_XSendEvent(dpy, w, propagate, event_mask, ev);

    static Atom net_active;  /* per-server constant, same rationale as above */
    if (!net_active)
        net_active = XInternAtom(dpy, "_NET_ACTIVE_WINDOW", False);

    /* Only gate _NET_ACTIVE_WINDOW requests the app itself sends to activate
     * (source 1 = application); leave our own pager-source messages (BELOW
     * add/remove, source 2) and other clients' messages alone. Uses the same
     * intent test as focus/raise so there is one policy, not two. */
    if (ev->xclient.message_type == net_active && ev->xclient.data.l[0] == 1) {
        char detail[96];
        const char *why;
        int intent = has_user_intent(dpy, detail, sizeof detail, &why);
        if (!intent && g_mode == MODE_ENFORCE) {
            logmsg("XSendEvent _NET_ACTIVE_WINDOW win=0x%lx %s → BLOCK (%s)",
                   (unsigned long)ev->xclient.window, detail, why);
            return 1;  /* deliberate deception, see XSetInputFocus */
        }
        logmsg("XSendEvent _NET_ACTIVE_WINDOW win=0x%lx %s → %s",
               (unsigned long)ev->xclient.window, detail,
               intent ? "allow" : "would BLOCK (log mode)");
    }
    return real_XSendEvent(dpy, w, propagate, event_mask, ev);
}

Window XCreateWindow(Display *dpy, Window parent, int x, int y, unsigned int width,
                     unsigned int height, unsigned int border_width, int depth,
                     unsigned int class, Visual *visual, unsigned long valuemask,
                     XSetWindowAttributes *attributes) {
    if (!real_XCreateWindow)
        real_XCreateWindow = must_sym("XCreateWindow");
    Window w = real_XCreateWindow(dpy, parent, x, y, width, height, border_width,
                                  depth, class, visual, valuemask, attributes);
    if (g_mode != MODE_OFF && w && is_root_window(dpy, parent)) {
        stamp_user_time_zero(dpy, w);
        logmsg("XCreateWindow 0x%lx (top-level) → stamped _NET_WM_USER_TIME=0",
               (unsigned long)w);
    }
    return w;
}

Window XCreateSimpleWindow(Display *dpy, Window parent, int x, int y,
                           unsigned int width, unsigned int height,
                           unsigned int border_width, unsigned long border,
                           unsigned long background) {
    if (!real_XCreateSimpleWindow)
        real_XCreateSimpleWindow = must_sym("XCreateSimpleWindow");
    Window w = real_XCreateSimpleWindow(dpy, parent, x, y, width, height,
                                        border_width, border, background);
    if (g_mode != MODE_OFF && w && is_root_window(dpy, parent)) {
        stamp_user_time_zero(dpy, w);
        logmsg("XCreateSimpleWindow 0x%lx (top-level) → stamped _NET_WM_USER_TIME=0",
               (unsigned long)w);
    }
    return w;
}

int XRaiseWindow(Display *dpy, Window w) {
    if (!real_XRaiseWindow)
        real_XRaiseWindow = must_sym("XRaiseWindow");
    if (g_mode == MODE_OFF)
        return real_XRaiseWindow(dpy, w);

    /* A raise is how TWS "pulls to the front" on launch/reconnect. Gate it
     * on the same intent test as focus: a self-initiated raise is the
     * takeover and is dropped in enforce mode. */
    char detail[96];
    const char *why;
    int intent = has_user_intent(dpy, detail, sizeof detail, &why);
    (void)why;

    if (intent || g_mode == MODE_LOG) {
        logmsg("XRaiseWindow win=0x%lx %s → %s",
               (unsigned long)w, detail,
               intent ? "allow" : "would BLOCK (log mode)");
        return real_XRaiseWindow(dpy, w);  /* log mode observes only */
    }

    logmsg("XRaiseWindow win=0x%lx %s → BLOCK (self-raise)", (unsigned long)w, detail);
    return 1;  /* success sentinel; the server never sees the raise */
}

int XMapRaised(Display *dpy, Window w) {
    if (!real_XMapRaised)
        real_XMapRaised = must_sym("XMapRaised");
    if (g_mode == MODE_OFF)
        return real_XMapRaised(dpy, w);

    /* XMapRaised = map + raise atomically. When self-initiated, downgrade to
     * a plain map so it does not raise. We do NOT try to force the window to
     * the back: app-side stacking control against AWT+Muffin proved unreliable
     * (BELOW deadlocks activation and restacks unpredictably; a plain lower
     * lands mid-stack; iconify fights AWT). A freshly-mapped window may still
     * appear on top at map; the focus/raise gating is what stops the takeover. */
    char detail[96];
    const char *why;
    int intent = has_user_intent(dpy, detail, sizeof detail, &why);
    (void)why;

    /* A self-map with no user intent is a launch/restart takeover candidate.
     * Queue it for the deferred main-window lower (5); the monitor drops it
     * if the title shows it is not the main window. */
    if (!intent)
        enqueue_lower(w);

    if (intent || g_mode == MODE_LOG) {
        logmsg("XMapRaised win=0x%lx %s → %s",
               (unsigned long)w, detail,
               intent ? "allow" : "map-as-window (log mode)");
        return real_XMapRaised(dpy, w);
    }

    if (!real_XMapWindow)
        real_XMapWindow = must_sym("XMapWindow");
    logmsg("XMapRaised win=0x%lx %s → map-without-raise (self-map)",
           (unsigned long)w, detail);
    return real_XMapWindow(dpy, w);
}

int XConfigureWindow(Display *dpy, Window w, unsigned int mask, XWindowChanges *changes) {
    if (!real_XConfigureWindow)
        real_XConfigureWindow = must_sym("XConfigureWindow");
    if (g_mode == MODE_OFF || !(mask & CWStackMode) || !changes)
        return real_XConfigureWindow(dpy, w, mask, changes);

    /* Only Above/TopIf/Opposite bring a window forward; Below/BottomIf don't. */
    if (changes->stack_mode != Above && changes->stack_mode != TopIf &&
        changes->stack_mode != Opposite)
        return real_XConfigureWindow(dpy, w, mask, changes);

    char detail[96];
    const char *why;
    int intent = has_user_intent(dpy, detail, sizeof detail, &why);
    (void)why;
    if (intent || g_mode == MODE_LOG) {
        logmsg("XConfigureWindow win=0x%lx stack=Above %s → %s",
               (unsigned long)w, detail,
               intent ? "allow" : "would strip stacking (log mode)");
        return real_XConfigureWindow(dpy, w, mask, changes);
    }

    /* Drop only the stacking request; keep any other changes in the call. */
    logmsg("XConfigureWindow win=0x%lx stack=Above → STRIP stacking (self-raise)",
           (unsigned long)w);
    return real_XConfigureWindow(dpy, w, mask & ~CWStackMode, changes);
}

int XNextEvent(Display *dpy, XEvent *ev) {
    if (!real_XNextEvent)
        real_XNextEvent = must_sym("XNextEvent");
    int r = real_XNextEvent(dpy, ev);
    track_event(ev);
    return r;
}

int XIfEvent(Display *dpy, XEvent *ev,
             Bool (*predicate)(Display *, XEvent *, XPointer), XPointer arg) {
    if (!real_XIfEvent)
        real_XIfEvent = must_sym("XIfEvent");
    int r = real_XIfEvent(dpy, ev, predicate, arg);
    track_event(ev);
    return r;
}

Bool XCheckIfEvent(Display *dpy, XEvent *ev,
                   Bool (*predicate)(Display *, XEvent *, XPointer), XPointer arg) {
    if (!real_XCheckIfEvent)
        real_XCheckIfEvent = must_sym("XCheckIfEvent");
    Bool r = real_XCheckIfEvent(dpy, ev, predicate, arg);
    if (r)
        track_event(ev);
    return r;
}
