FROM nginx:alpine

# Copiar la app al directorio que sirve nginx
COPY index.html /usr/share/nginx/html/index.html

# Configuración de nginx para SPA + proxy al backend
COPY nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 80
