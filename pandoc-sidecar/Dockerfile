# syntax=docker/dockerfile:1.7
# Alpine 3.24 ships pandoc 3.10; bumping the Alpine version can change pandoc.
FROM python:3.14-alpine3.24
RUN apk add --no-cache pandoc tzdata
ADD --checksum=sha256:28fca7ae6ebc7ed7bb63bde63136a74bfef14f296a57e403657eeb8b32836073 \
    https://cdn.jsdelivr.net/npm/mermaid@12.0.0/dist/mermaid.min.js /app/mermaid.min.js
COPY server.py nobreak.html break.html compact.html obsidian.lua callouts.lua mermaid.lua copycode.html mermaid.html /app/
WORKDIR /app
CMD ["python3", "/app/server.py"]
