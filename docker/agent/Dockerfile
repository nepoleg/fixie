FROM eclipse-temurin:17-jdk-noble

RUN apt-get update && apt-get install -y --no-install-recommends \
      git curl ca-certificates iptables unzip \
    && curl -fsSL https://deb.nodesource.com/setup_24.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

RUN npm install -g @anthropic-ai/claude-code && npm cache clean --force

RUN useradd --create-home --shell /bin/bash agent

RUN git config --system user.name fixie \
    && git config --system user.email fixie@localhost \
    && git config --system safe.directory /mirror/monorepo.git

USER agent
RUN D=/home/agent/.gradle/wrapper/dists/gradle-8.12-bin/cetblhg4pflnnks72fxwobvgv \
    && mkdir -p "$D" \
    && curl -fsSL -o /tmp/gradle.zip https://services.gradle.org/distributions/gradle-8.12-bin.zip \
    && echo "7a00d51fb93147819aab76024feece20b6b84e420694101f276be952e08bef03  /tmp/gradle.zip" | sha256sum -c - \
    && unzip -q /tmp/gradle.zip -d "$D" && rm /tmp/gradle.zip && touch "$D/gradle-8.12-bin.zip.ok" \
    && printf 'org.gradle.java.installations.auto-download=false\n' > /home/agent/.gradle/gradle.properties
USER root

COPY --chmod=755 entrypoint.sh init-firewall.sh run-ticket.sh fake-agent.sh /opt/fixie/
COPY --chmod=644 agent-rules.md /opt/fixie/CLAUDE.md

ENV GRADLE_RO_DEP_CACHE=/gradle-ro \
    DISABLE_AUTOUPDATER=1 \
    DISABLE_TELEMETRY=1 \
    DISABLE_ERROR_REPORTING=1 \
    CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1

WORKDIR /home/agent
ENTRYPOINT ["/opt/fixie/entrypoint.sh"]
CMD ["/opt/fixie/run-ticket.sh"]
