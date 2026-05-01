# src/frontend/Dockerfile
FROM nginxinc/nginx-unprivileged:1.30.0-alpine

# remove default config + create required writable dirs (do not chown /tmp itself)
RUN rm -f /etc/nginx/conf.d/default.conf \
 && mkdir -p /tmp/nginx /tmp/client_body /tmp/proxy /tmp/fastcgi /tmp/uwsgi /tmp/scgi

# copy nginx.conf and static files, set ownership at copy time to avoid chown errors
COPY --chown=nginx:nginx nginx.conf /etc/nginx/nginx.conf
COPY --chown=nginx:nginx index.html /usr/share/nginx/html/index.html
COPY --chown=nginx:nginx assets /usr/share/nginx/html/assets

# expose the unprivileged port used by the image
EXPOSE 8080

# run as the non-root nginx user provided by the base image
USER nginx

CMD ["nginx", "-g", "daemon off;"]
