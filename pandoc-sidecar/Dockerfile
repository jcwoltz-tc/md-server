FROM python:3.12-alpine
RUN apk add --no-cache pandoc
ADD https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.min.js /app/mermaid.min.js
COPY server.py nobreak.html break.html compact.html callouts.lua mermaid.lua copycode.html mermaid.html /app/
WORKDIR /app
CMD ["python3", "/app/server.py"]
