@echo off
rem ============================================================
rem  Anti-fraud Experience Launcher (fan-zha yan-lian-chang)
rem  Opens the DeepSeek-style console served on s1 via SSH tunnel.
rem  - Tunnel: local 8090 -> s1 127.0.0.1:8090 (real model)
rem  - Keep the ssh window open while playing; closing it ends access.
rem  - First visit: paste the access key once in the sidebar
rem    (bottom-left input) - the browser remembers it.
rem ============================================================
start "experience-tunnel" ssh -N -L 8090:127.0.0.1:8090 -i "%USERPROFILE%\.ssh\acps-hk-key.pem" -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -o StrictHostKeyChecking=accept-new ubuntu@150.109.154.16
timeout /t 2 /nobreak >nul
start "" http://127.0.0.1:8090
