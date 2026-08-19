# CyberQwen Kali sandbox — Kali rolling with a comprehensive pentest toolset
# baked in, so tools are preinstalled and survive container recreation.
#
# Build:  docker build -t cyberqwen-kali kali_sandbox/
# (setup.sh does this automatically and runs kali-mcp-sandbox from it.)
#
# Grouped by category on purpose: each RUN is its own layer, so if one package
# name has drifted in Kali rolling, only that group fails and the earlier layers
# stay cached — comment out the offending package and rebuild.
#
# SIZE WARNING: with all groups this image is ~3-6 GB. To slim it down, comment
# out Group 6 (Metasploit) and/or the wordlists in Group 5.

FROM kalilinux/kali-rolling

ENV DEBIAN_FRONTEND=noninteractive

# ── Group 1: base utilities / networking ────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
      curl wget git jq vim nano ca-certificates \
      python3 python3-pip python3-venv pipx \
      net-tools iproute2 iputils-ping dnsutils \
      netcat-traditional socat tcpdump \
    && rm -rf /var/lib/apt/lists/*

# ── Group 2: reconnaissance / enumeration ───────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
      nmap masscan \
      dnsrecon dnsenum whois \
      theharvester recon-ng \
      arp-scan netdiscover nbtscan \
      onesixtyone snmp snmpcheck \
    && rm -rf /var/lib/apt/lists/*

# ── Group 3: SMB / Active Directory ─────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
      smbclient smbmap enum4linux enum4linux-ng \
      ldap-utils \
      crackmapexec impacket-scripts evil-winrm responder \
    && rm -rf /var/lib/apt/lists/*
# NOTE: if 'crackmapexec' fails (upstream renamed it), replace with 'netexec'.

# ── Group 4: web application testing ────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
      gobuster ffuf feroxbuster dirb \
      nikto whatweb wafw00f wfuzz \
      sqlmap commix wpscan nuclei sslscan \
    && rm -rf /var/lib/apt/lists/*

# ── Group 5: passwords / wordlists ──────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
      hydra john hashcat medusa hashid \
      seclists wordlists \
    && rm -rf /var/lib/apt/lists/*
# 'wordlists' + 'seclists' are large (~1 GB+). Comment out to slim the image.
# rockyou.txt ships gzipped at /usr/share/wordlists/rockyou.txt.gz — unzip once:
RUN [ -f /usr/share/wordlists/rockyou.txt.gz ] && gunzip -k /usr/share/wordlists/rockyou.txt.gz || true

# ── Group 6: exploitation (HEAVY — Metasploit is ~1.5 GB) ───────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
      metasploit-framework exploitdb proxychains4 \
    && rm -rf /var/lib/apt/lists/*
# To slim the image significantly, comment out this whole group; the agent can
# still install msf on demand via bash (subject to operator approval).

# keep the container alive; mcp_server.py runs commands via `docker exec`
CMD ["tail", "-f", "/dev/null"]
