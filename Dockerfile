# A base traz ROS 2 Humble, vida_interfaces e vida_navigation, na mesma versao do
# notebook. Recompile este painel depois de um "docker compose build" do alto-nivel.
FROM vida-ros-ws:humble

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl gnupg iputils-ping python3-aiohttp python3-pytest \
    && install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
        > /etc/apt/sources.list.d/docker.list \
    && apt-get update && apt-get install -y --no-install-recommends docker-ce-cli docker-compose-plugin \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /painel
COPY painel ./painel
COPY web ./web
COPY tests ./tests

CMD ["bash", "-lc", "source /opt/ros/humble/setup.bash && source /vida_ws/install/setup.bash && exec python3 -m painel.server"]
