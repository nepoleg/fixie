#!/bin/bash
# Locks outbound traffic to the Anthropic API, ADO (read-only token) and dependency feeds, self-tests, then returns.
set -euo pipefail

HOSTS=(api.anthropic.com dev.azure.com pkgs.dev.azure.com europe-maven.pkg.dev)
IPV4_RE='^([0-9]{1,3}\.){3}[0-9]{1,3}$'

ALL_IPS=()
for host in "${HOSTS[@]}"; do
  ips=$(getent ahostsv4 "$host" 2>/dev/null | awk '{print $1}' | sort -u | grep -E "$IPV4_RE" || true)
  if [ -z "$ips" ]; then
    echo "fixie-firewall: cannot resolve $host" >&2
    exit 1
  fi
  while IFS= read -r ip; do
    ALL_IPS+=("$ip")
  done <<< "$ips"
done

mapfile -t RESOLVERS < <(awk '/^nameserver/ {print $2}' /etc/resolv.conf | grep -E "$IPV4_RE" || true)

iptables -F INPUT
iptables -F OUTPUT
iptables -F FORWARD

iptables -A INPUT -i lo -j ACCEPT
iptables -A INPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
iptables -P INPUT DROP

iptables -P FORWARD DROP

iptables -A OUTPUT -o lo -j ACCEPT
iptables -A OUTPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
for resolver in "${RESOLVERS[@]}"; do
  iptables -A OUTPUT -p udp -d "$resolver" --dport 53 -j ACCEPT
  iptables -A OUTPUT -p tcp -d "$resolver" --dport 53 -j ACCEPT
done

declare -A seen
unique_ips=()
for ip in "${ALL_IPS[@]}"; do
  if [ -z "${seen[$ip]:-}" ]; then
    seen[$ip]=1
    unique_ips+=("$ip")
  fi
done
for ip in "${unique_ips[@]}"; do
  iptables -A OUTPUT -p tcp -d "$ip" --dport 443 -j ACCEPT
done

iptables -A OUTPUT -j REJECT --reject-with icmp-admin-prohibited
iptables -P OUTPUT DROP

if ip6tables -S >/dev/null 2>&1; then
  ip6tables -F INPUT
  ip6tables -F OUTPUT
  ip6tables -F FORWARD
  ip6tables -A INPUT -i lo -j ACCEPT
  ip6tables -A OUTPUT -o lo -j ACCEPT
  ip6tables -P INPUT DROP
  ip6tables -P OUTPUT DROP
  ip6tables -P FORWARD DROP
  echo "fixie-firewall: ipv6 blocked" >&2
elif [ -s /proc/net/if_inet6 ] && awk '{ if ($NF != "lo") found=1 } END { exit !found }' /proc/net/if_inet6; then
  echo "fixie-firewall: self-test failed: ipv6 present but ip6tables unavailable" >&2
  exit 1
else
  echo "fixie-firewall: ipv6 unavailable" >&2
fi

echo "fixie-firewall: allowed ${#unique_ips[@]} addresses" >&2

if ! curl -sS -o /dev/null --max-time 10 https://api.anthropic.com; then
  echo "fixie-firewall: self-test failed: api.anthropic.com unreachable" >&2
  exit 1
fi
if curl -sS -o /dev/null --max-time 10 https://example.com; then
  echo "fixie-firewall: self-test failed: example.com reachable" >&2
  exit 1
fi
echo "fixie-firewall: self-test ok" >&2
