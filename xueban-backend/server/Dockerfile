# Xueban Node.js gateway backend.
# Development image: compose mounts ./server and runs tsx watch.

FROM node:22-alpine

WORKDIR /app

# Toolchain for native dependencies and compatibility libraries.
RUN apk add --no-cache python3 make g++ libc6-compat

COPY package.json package-lock.json ./
RUN npm ci

COPY . .

EXPOSE 8002

CMD ["npm", "run", "dev"]
