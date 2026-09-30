***REMOVED*** Frontend Dockerfile for Vue 3 + Vite dev server

FROM node:20-alpine

WORKDIR /app

***REMOVED*** Install dependencies
COPY package.json package-lock.json* ./
RUN npm ci || npm install

***REMOVED*** Copy source
COPY . .

EXPOSE 5173

CMD ["npm", "run", "dev", "--", "--host", "0.0.0.0", "--port", "5173"]
