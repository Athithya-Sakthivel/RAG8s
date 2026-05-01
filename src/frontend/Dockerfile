FROM nginxinc/nginx-unprivileged:1.30.0-alpine@sha256:35a4455cd4153d7cd0ae1fcfa42451884ef19848202606f144d9a089376c7e6c

# remove default config + create required writable dirs (do not chown /tmp itself)
RUN rm -f /etc/nginx/conf.d/default.conf \
 && mkdir -p /tmp/nginx /tmp/client_body /tmp/proxy /tmp/fastcgi /tmp/uwsgi /tmp/scgi \
 && chown -R nginx:nginx /tmp/nginx /tmp/client_body /tmp/proxy /tmp/fastcgi /tmp/uwsgi /tmp/scgi

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
