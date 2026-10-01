# Inicio de Social Pod en Windows

## Uso diario: SAFE

1. La primera vez, hacé doble clic en `INSTALL_SOCIAL_POD.cmd`.
2. Después, hacé doble clic en `START_SOCIAL_POD.cmd`.
3. Esperá `Camoufox Profile Manager: ONLINE` y `Execution: SAFE`.
4. Usá el menú `SOCIAL POD — X OPERATOR`.
5. Al terminar, elegí `8. Salir`.

SAFE fuerza `SOCIAL_POD_EXECUTION_ENABLED=false` para esa sesión. No puede publicar contenido real, incluso si otra ventana había usado modo REAL.

## Uso con ejecución real

1. Hacé doble clic en `START_SOCIAL_POD_REAL.cmd`.
2. Leé la advertencia y escribí `REAL` para continuar (no distingue mayúsculas/minúsculas).
3. Esperá `Camoufox Profile Manager: ONLINE` y `Execution: REAL`.
4. En la opción 5 revisá el preview y escribí `PUBLICAR` para cada post individual.
5. Al cerrar la ventana, el modo REAL desaparece: no queda guardado en Windows.

REAL permite acciones externas en cuentas conectadas. La confirmación `REAL` habilita la sesión y la confirmación `PUBLICAR` sigue siendo obligatoria para cada publicación.

Camoufox queda abierto para el próximo uso. Para cerrarlo manualmente, cerrá su ventana o proceso en el puerto configurado (por defecto, `127.0.0.1:8000`).
