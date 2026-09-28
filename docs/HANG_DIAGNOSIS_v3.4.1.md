# 🔍 Debug Session: server-startup-hang
**Status:** `[OPEN]`  
**Session ID:** `server-startup-hang`  
**Date:** 2026-09-20  
**Reporter:** user  
**Symptom:** `python server_main.py` → **полная тишина 5+ минут**, нет никакого вывода в консоль, Ctrl+C может прервать.  
**Expected:** Печать приветствия / IP-приглашений / старт HTTP-сервера за 1-5 секунд.  
**Environment:** Windows, Python 3.x, Friend Relay v3.4.1

---

## 🎯 5 Falsifiable Hypotheses (3 HIGH + 2 MEDIUM)
| # | Hypothesis | Likelihood | Evidence Point |
|---|---|---:|---|
| H1 | `lib/net_utils.get_interfaces()` зависает на **одной из 5 сетевых операций**: WinAPI SIO_GET_INTERFACE_LIST, subprocess `netsh`, socket.getaddrinfo(hostname), socket.connect(8.8.8.8:80), socket.if_nameindex() → **HIGH 75%** | 75% | `NET_*` точки в net_utils.py |
| H2 | `_netsh_names_map()` subprocess timeout=1 не работает на этой Windows (процесс netsh порождает дочерние, Python subprocess.timeout их не убивает) → зависает навсегда | **HIGH 60%** | `NET_NETSH_START` |
| H3 | Циклический импорт: `net_utils.py` → `tls_util.py` → `relay_server.py` → `net_utils.py` (deadlock import lock) | 35% | `IMPORT_*` точки в server_main.py |
| H4 | Генерация TLS-сертификата RSA 2048 в `tls_util.gen_certs()` висит из-за нехватки энтропии на слабом CPU | 20% | `TLS_GEN_*` точки |
| H5 | Один из **import-time** выражений (не внутри функции) выполняет тяжёлую I/O → зависает до первой строчки кода | 15% | `IMPORT_*` + `BOOT_*` точки |

---

## 📜 Evidence Log
| Timestamp | Pre-fix / Post-fix | Instrumentation Point | Status |
|---|---|---|---|
| (pending) | pre-fix | | |

---

## 🔧 Fix Plan (after evidence)
Pending — TBD after H# confirmed.
