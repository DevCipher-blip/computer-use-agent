# Build the base image first: docker build -t cambioml/computer-use-base:latest ./computer-use-demo
FROM cambioml/computer-use-base:latest

ENV PYTHONIOENCODING=utf-8 LANG=C.UTF-8 LC_ALL=C.UTF-8

# WORKDIR is /home/computeruse (inherited from base image)
# ENTRYPOINT is ["./entrypoint.sh"] (inherited, runs /home/computeruse/entrypoint.sh)

# Install session agent dependencies on top of base image
COPY requirements.txt session_agent_requirements.txt
RUN pip install --no-cache-dir -r session_agent_requirements.txt

# Copy session agent source
COPY session_agent.py session_agent.py

# Overwrite the original entrypoint.sh — starts session_agent instead of Streamlit
COPY --chmod=755 entrypoint.sh entrypoint.sh

# 8765 = session agent HTTP/WS API
# 5900 = VNC direct
# 6080 = noVNC browser access
EXPOSE 8765 5900 6080

HEALTHCHECK --interval=5s --timeout=3s --retries=20 \
    CMD curl -f http://localhost:8765/health || exit 1
