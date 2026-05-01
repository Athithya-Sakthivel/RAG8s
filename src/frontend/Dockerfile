# src/frontend/Dockerfile
FROM nginxinc/nginx-unprivileged:1.30.0-alpine

# remove default config + create required writable dirs
RUN rm -f /etc/nginx/conf.d/default.conf \
 && mkdir -p /tmp/nginx /tmp/client_body /tmp/proxy /tmp/fastcgi /tmp/uwsgi /tmp/scgi \
 && chown -R nginx:nginx /tmp

# static files
COPY nginx.conf /etc/nginx/nginx.conf
COPY index.html /usr/share/nginx/html/index.html
COPY assets /usr/share/nginx/html/assets

# ensure runtime files are owned by the non-root nginx user
RUN chown -R nginx:nginx /usr/share/nginx/html /etc/nginx

EXPOSE 8080

# required to avoid false positive security scans
USER nginx

CMD ["nginx", "-g", "daemon off;"]
