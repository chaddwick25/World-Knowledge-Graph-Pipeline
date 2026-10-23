# Edge image: multi-stage. Stage 1 builds the SPA; stage 2 serves dist
# from the base edge image. The edge config is operator-supplied at
# runtime (mounted template, rendered by the base image's envsubst
# mechanism), so the image carries no topology. The SPA root is a build
# arg paired with the runtime env of the same name (the rendered config
# points root at it); the default keeps the stock layout.
ARG SPA_ROOT=/usr/share/nginx/html
FROM node:20 AS spa-build
WORKDIR /src
COPY package.json package-lock.json ./
RUN npm ci
ARG VITE_API_BASE_URL=/api
ARG VITE_CARTO_API_KEY
ENV VITE_API_BASE_URL=$VITE_API_BASE_URL \
    VITE_CARTO_API_KEY=$VITE_CARTO_API_KEY
COPY . .
RUN npm run build

FROM nginx:alpine
ARG SPA_ROOT
COPY --from=spa-build /src/dist ${SPA_ROOT}/
