FROM nginxinc/nginx-unprivileged:1.30.0-alpine

# remove default config + create required writable dirs
RUN rm -f /etc/nginx/conf.d/default.conf \
    && mkdir -p /tmp/nginx \
    /tmp/client_body \
    /tmp/proxy \
    /tmp/fastcgi \
    /tmp/uwsgi \
    /tmp/scgi

# static files
COPY nginx.conf /etc/nginx/nginx.conf
COPY index.html /usr/share/nginx/html/index.html
COPY assets /usr/share/nginx/html/assets

EXPOSE 8080

CMD ["nginx", "-g", "daemon off;"]