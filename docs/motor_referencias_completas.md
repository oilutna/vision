# Motor de referencias completas

Base: oilutna/vision, commit 728089a5a7eff28f9787eaa7b5d3de4cc6daee62, versión de interfaz 5.5.1. Corrección centrada en el motor, con interfaz conservada.

Se entrega un único Python ejecutable. Mantiene las pantallas, clases de interfaz, cámara, autenticación y almacenamiento sin cambios estructurales (verificado por comparación de AST). Las dependencias permanecen iguales.

## Comparación

- Las diez OK permanecen como diez referencias completas. Se selecciona como patrón una fotografía real alineada representativa (medoide), sin promedio por píxel de las piezas. La medición conserva suavizado de ruido, pero la imagen de referencia visible no se forma fusionando versiones.
- Se localiza la pieza buscando contra las referencias y solo se comparan las que coinciden en la ubicación del mismo objeto. Cada evaluación exige que todas las zonas aprueben contra una misma referencia.
- La dispersión entre versiones distintas no aumenta los umbrales por píxel ni los límites de defectos. Se conserva el control de sensibilidad por zona, pero la tolerancia efectiva cambia respecto al patrón estadístico anterior: es necesario comprobarla de nuevo.
- La comparación visible muestra la referencia concreta que se utilizó, además de la región alineada/normalizada correspondiente. No hay nuevas ventanas o controles.
- Si la alineación fina no es confiable no se emite decisión. Se conservan los límites geométricos de alineación del motor anterior.
- Cada frame de acumulación se evalúa. Cambiar de referencia o decisión de zona reinicia la acumulación, evitando diluir el defecto final con frames buenos anteriores.
- Se registra el índice y ruta de la referencia elegida en las métricas existentes. El identificador de motor es complete-reference-engine-1.

## Uso y validación

Respalde el Python anterior. Coloque el nuevo archivo en la misma carpeta de su programa para conservar la ubicación relativa de `datos`. Puede renombrarlo como `poka_yoke_mejorado5.py`. No elimine ni cambie la carpeta de datos.

Las aprobaciones anteriores quedan pendientes por cambio de huella del motor. Abra el modelo, compruebe las diez OK y diez NG y habilite la versión después de revisar los resultados. No es obligatorio recapturar fotos utilizables; si las referencias están borrosas o no representan las variantes permitidas, deben reemplazarse. El software no puede recuperar detalles que la cámara no capturó.

34 pruebas enfocadas aprobadas: 8 regresiones nuevas y 26 pruebas originales de motor/diagnóstico/comparación. Incluyen ambas variantes aceptables, rechazo de híbridos, desplazamiento, fallo de alineación, defecto en último frame, referencia visible y conservación de las clases de UI/cámara/almacenamiento. Se utilizó Python 3.13/OpenCV 4.14/CustomTkinter 5.2.2. No se ejecutó de nuevo la suite gráfica completa de 102 casos. Para las pruebas originales se forzó recolección de memoria antes de limpiar directorios temporales; no se corrigió ni alteró el almacenamiento de producción.

Los problemas de retirada basada en coincidencia y cierre de SQLite de la revisión anterior no se modificaron por el alcance solicitado. La normalización de color sigue siendo configurable: no debe asumirse que diferencias uniformes de color/acabado serán detectadas con ella activada.

**La eficacia necesita validación con piezas físicas reales**, en cada variante, defecto, iluminación y posición. Estas pruebas sintéticas no garantizan una precisión industrial determinada. Una referencia completa evita híbridos estadísticos; no reemplaza definir tolerancias, defectos mínimos detectables y criterios de aceptación con Calidad.
