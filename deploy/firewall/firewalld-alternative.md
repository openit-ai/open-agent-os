# Firewalld alternative — §16A.6 Network Isolation
# When the host uses firewalld instead of raw nftables, use this as the
# equivalent hermes egress policy. Pick ONE backend (nftables OR firewalld).

# ---------------------------------------------------------------------------
# Option A: firewalld direct rules (persistent)
# Requires firewalld with direct interface (iptables backend) or
# firewalld >= 1.0 with policy objects.
# ---------------------------------------------------------------------------

# 1) Ensure firewalld is running
# sudo systemctl enable --now firewalld

# 2) Create an ipset for allowed LLM/update destinations (customize IPs)
# sudo firewall-cmd --permanent --new-ipset=llm_allow --type=hash:net
# sudo firewall-cmd --permanent --ipset=llm_allow --add-entry=34.0.0.0/8
# sudo firewall-cmd --permanent --ipset=llm_allow --add-entry=34.120.0.0/13

# 3) Tag hermes traffic via cgroup or uid with direct rules.
#    firewalld direct rules are passthrough to iptables/nft — uid match via mangle:
#
# sudo firewall-cmd --permanent --direct --add-rule ipv4 filter OUTPUT 0 \
#   -m owner --uid-owner hermes -o lo -j ACCEPT
# sudo firewall-cmd --permanent --direct --add-rule ipv4 filter OUTPUT 1 \
#   -m owner --uid-owner hermes -d 10.20.0.0/16 -j DROP
# sudo firewall-cmd --permanent --direct --add-rule ipv4 filter OUTPUT 1 \
#   -m owner --uid-owner hermes -d 10.30.0.0/16 -j DROP
# sudo firewall-cmd --permanent --direct --add-rule ipv4 filter OUTPUT 1 \
#   -m owner --uid-owner hermes -d 10.40.0.0/16 -j DROP
# sudo firewall-cmd --permanent --direct --add-rule ipv4 filter OUTPUT 1 \
#   -m owner --uid-owner hermes -d 10.50.0.0/16 -j DROP
# sudo firewall-cmd --permanent --direct --add-rule ipv4 filter OUTPUT 1 \
#   -m owner --uid-owner hermes -p tcp --dport 22 -d 10.0.0.0/8 -j DROP
# sudo firewall-cmd --permanent --direct --add-rule ipv4 filter OUTPUT 1 \
#   -m owner --uid-owner hermes -p tcp --dport 5432 -j DROP
# sudo firewall-cmd --permanent --direct --add-rule ipv4 filter OUTPUT 1 \
#   -m owner --uid-owner hermes -p tcp --dport 3306 -j DROP
# sudo firewall-cmd --permanent --direct --add-rule ipv4 filter OUTPUT 1 \
#   -m owner --uid-owner hermes -p tcp --dport 6379 -j DROP
# sudo firewall-cmd --permanent --direct --add-rule ipv4 filter OUTPUT 2 \
#   -m owner --uid-owner hermes -d 10.10.1.10 -p tcp --dport 8000 -j ACCEPT
# sudo firewall-cmd --permanent --direct --add-rule ipv4 filter OUTPUT 2 \
#   -m owner --uid-owner hermes -d 10.10.1.11 -p tcp --dport 8001 -j ACCEPT
# sudo firewall-cmd --permanent --direct --add-rule ipv4 filter OUTPUT 3 \
#   -m owner --uid-owner hermes -p udp --dport 53 -j ACCEPT
# sudo firewall-cmd --permanent --direct --add-rule ipv4 filter OUTPUT 3 \
#   -m owner --uid-owner hermes -p tcp --dport 53 -j ACCEPT
# sudo firewall-cmd --permanent --direct --add-rule ipv4 filter OUTPUT 4 \
#   -m owner --uid-owner hermes -p tcp --dport 443 -j ACCEPT
#   # TODO: restrict 443 to llm_allow ipset in production:
#   # -m set --match-set llm_allow dst -p tcp --dport 443
# sudo firewall-cmd --permanent --direct --add-rule ipv4 filter OUTPUT 99 \
#   -m owner --uid-owner hermes -j DROP
# sudo firewall-cmd --reload
# sudo firewall-cmd --direct --get-all-rules

# ---------------------------------------------------------------------------
# Option B: firewalld policy objects (firewalld >= 1.0, nft backend)
# More declarative — create an egress policy for the hermes zone.
# ---------------------------------------------------------------------------
# sudo firewall-cmd --permanent --new-policy=hermes-egress
# sudo firewall-cmd --permanent --policy=hermes-egress --set-target=DROP
# sudo firewall-cmd --permanent --policy=hermes-egress --add-ingress-zone=HOST
# sudo firewall-cmd --permanent --policy=hermes-egress --add-egress-zone=ANY
# # Then add rich rules / services as needed; uid matching still requires
# # direct/nft passthrough — combine with Option A for uid granularity.

# ---------------------------------------------------------------------------
# Verification (both options)
# ---------------------------------------------------------------------------
# sudo firewall-cmd --direct --get-all-rules | grep hermes
# sudo -u hermes curl -v https://api.openai.com/v1/models  # should succeed (LLM allow)
# sudo -u hermes curl -v --connect-timeout 3 10.20.0.5:5432 # should timeout/drop (DB deny)
# sudo -u hermes nc -vz 10.30.0.5 443                       # should drop (ERP deny)

# ---------------------------------------------------------------------------
# Notes
# ---------------------------------------------------------------------------
# - Customize 10.10.1.10:8000 (ACP), 10.10.1.11:8001 (MCP), and llm_allow CIDRs
#   per customer network before applying.
# - Prefer nftables (hermes-egress.nft) on hosts without firewalld — it is
#   more explicit and easier to audit. Use this file only when firewalld is
#   the host's authoritative firewall.
