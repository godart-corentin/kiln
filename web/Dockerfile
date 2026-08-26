FROM node:22-alpine AS frontend
WORKDIR /src
COPY frontend/package*.json ./
RUN if [ -f package-lock.json ]; then npm ci --no-audit --no-fund; else npm install --no-audit --no-fund; fi
COPY frontend/ ./
RUN npm run build

FROM python:3.12-alpine
WORKDIR /opt/kiln
COPY server/kiln_web.py /opt/kiln/web
COPY --from=frontend /src/dist /opt/kiln/static
CMD ["python3", "/opt/kiln/web"]
