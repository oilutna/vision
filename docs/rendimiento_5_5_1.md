# Rendimiento de inspección · 5.5.1

Se comparó el código de 5.5.0 (`3953aea`) con 5.5.1 en el mismo entorno Linux de nube, con Tk en una pantalla virtual. OpenCV se fijó a un hilo sólo para medir de forma consistente; la aplicación no modifica la cantidad de hilos de OpenCV.

| Medición | 5.5.0 | 5.5.1 |
| --- | ---: | ---: |
| Dibujo de un cuadro FHD en un área de 640 × 360, mediana de 20 repeticiones | 22,768 ms | 2,950 ms |
| Tiempo hasta mostrar OK, cámara simulada a 30 FPS, mediana de 3 presentaciones | 1375,6 ms | 484,8 ms |
| Cálculo de un ciclo de 14 cuadros FHD, mediana de 3 repeticiones | 239,918 ms | 232,052 ms |

El motor y sus criterios permanecen iguales; la pequeña diferencia del cálculo corresponde a variación entre ejecuciones. Las mejoras principales están en la espera entre tareas, el dibujo y la gestión de controles.

## Qué se midió

- **Dibujo:** cuadro BGR de 1920 × 1080 generado con una semilla fija, mostrado mediante `ImageCanvas` en 640 × 360. Se mantiene el cuadro original en memoria; sólo se reduce la representación visual. La medición incluye la creación de la imagen Tk.
- **Tiempo de respuesta:** componente sintético de `tests.helpers.images`, cuadro de 240 × 200 y cámara simulada con timestamps a 30 FPS. Se presenta la pieza después del retiro confirmado y se mide hasta que el texto de resultado muestra OK. Se repite tres veces con la misma configuración. También se espera la escritura del evento antes de presentar la siguiente pieza.
- **Cálculo:** cuadro completo de 1920 × 1080 y región de 400 × 500, con seis cuadros de estabilización y ocho de promedio. Se usa el mismo patrón y configuración en ambas versiones.

Resultados individuales hasta mostrar OK:

- 5.5.0: 1567,1; 1355,5; 1375,6 ms.
- 5.5.1: 502,1; 484,8; 481,0 ms.

El recorrido de controles administrativos costó alrededor de 15,5 ms por llamada en este entorno. Ahora sólo se habilitan o deshabilitan al realizar una operación administrativa, sin repetir ese recorrido después de cada cuadro de producción.

## Verificación de comportamiento

Las pruebas automatizadas comprueban que:

- la cámara sigue actualizándose durante un cálculo lento;
- el resultado puede mostrarse antes de terminar una escritura lenta;
- el contador cambia cuando se guarda el registro y cuenta una sola vez;
- el cierre no descarta una escritura pendiente;
- el dibujo no reduce la resolución de entrada al inspector;
- las imágenes y mediciones conservan su propiedad de memoria y los estados de desconexión;
- los criterios OK/NG, referencias, estabilización, promedio y retiro mantienen sus pruebas anteriores.

La velocidad en un equipo físico depende de la cámara, FPS reales, exposición, tamaño de región, CPU, resolución y almacenamiento. Estas mediciones no equivalen a una prueba del dispositivo del usuario.
