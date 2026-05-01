FROM nginxinc/nginx-unprivileged:1.30.0-alpine

# remove default config + prepare writable dirs
RUN rm -f /etc/nginx/conf.d/default.conf \
 && mkdir -p /tmp/nginx /tmp/client_body /tmp/proxy /tmp/fastcgi /tmp/uwsgi /tmp/scgi \
 && chmod 0777 /tmp/nginx /tmp/client_body /tmp/proxy /tmp/fastcgi /tmp/uwsgi /tmp/scgi

# only static assets go into image
COPY --chown=nginx:nginx index.html /usr/share/nginx/html/index.html
COPY --chown=nginx:nginx assets /usr/share/nginx/html/assets

EXPOSE 8080

# Explicitly set nginx user to avoid false positives in security scans
USER nginx

CMD ["nginx", "-g", "daemon off;"]

