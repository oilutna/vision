# Poka-Yoke 5

Aplicación local de inspección visual para ensambles, con Administración e Inspección separadas. El programa completo está en **poka_yoke_mejorado5.py**, sin módulos propios adicionales.

## Ejecutar

Recomendado: Python 3.12 de 64 bits, con Tkinter. Instala las dependencias desde esta carpeta:

```sh
python -m pip install -r requirements.txt
python poka_yoke_mejorado5.py
```

En Windows puedes usar `py -3` en lugar de `python`.

## Uso inicial

1. Abre Administración y establece tu contraseña local (mínimo ocho caracteres).
2. Crea un número de parte o usa **Importar modelos v3/v4** para copiar tus modelos anteriores sin modificar los originales.
3. Configura la cámara y su resolución real, región general, diez referencias OK y tres o cinco zonas con sus nombres y límites.
4. Prueba los ajustes y guarda la configuración. Puedes descartar el borrador o recuperar una versión anterior.
5. Agrega muestras independientes de al menos diez piezas OK y diez NG. Identifica cada pieza física; repetir fotos de la misma pieza no cuenta como piezas distintas.
6. Guarda, ejecuta las pruebas y revisa los resultados antes de habilitar la versión para inspección.
7. Sal de Administración, selecciona el número de parte e introduce operador y lote para iniciar.

Las muestras NG se guardan separadas de las referencias OK. Los cambios de configuración requieren nueva validación. Una muestra sin evaluación se distingue de una aceptación o rechazo.

La interfaz permite buscar números de parte, gestionar referencias, guardar y recuperar ajustes, importar/exportar modelos y consultar el historial con fotografías y reportes CSV.

## Datos

Por defecto se guardan modelos, versiones, contraseña y registros en `datos`, junto al archivo Python. La ubicación no depende de la carpeta desde la que se ejecuta. Puedes cambiarla:

```sh
python poka_yoke_mejorado5.py --data-dir "D:/PokaYokeDatos"
```

La carpeta de datos está excluida de Git. Para respaldar todo, cierra el programa y copia esa carpeta. Exportar un modelo conserva su receta actual e imágenes, pero no incluye la contraseña ni el historial completo.

## Verificación y límites

Esta versión de archivo único pasó 45 pruebas automatizadas, incluidas pruebas de interfaz con cámara simulada y del motor OpenCV sobre imágenes sintéticas. No se verificó una cámara física ni su controlador de Windows.

Comprueba en tu equipo enfoque, iluminación, resolución, velocidad y resultados con piezas reales. Las pruebas iniciales no garantizan una tasa de detección para todos los defectos. La presencia y el retiro se estiman visualmente; no hay un sensor físico de presencia. El motor indica diferencias por zona, sin diagnosticar automáticamente el tipo de defecto.
