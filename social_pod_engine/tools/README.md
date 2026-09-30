# X Operator

Ejecutá `python -m social_pod_engine.tools.x_operator`.

1. Elegí o verificá una cuenta X.
2. Para onboarding o reautenticación, iniciá el servicio con un gateway Camoufox inyectado; el login, password y 2FA se realizan únicamente en la ventana del browser.
3. Ejecutá healthcheck.
4. Para un post de prueba, escribí el texto, revisá el preview y confirmá exactamente `PUBLICAR`.

El menú nunca pide, muestra ni guarda credenciales. No ejecuta posts si no hay un servicio de smoke test explícitamente inyectado.
