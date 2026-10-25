# Poka-Yoke 5

Versión **5.0.1**. Aplicación local de inspección visual para ensambles, con Administración e Inspección separadas. El programa completo está en **poka_yoke_mejorado5.py**, sin módulos propios adicionales.

## Ejecutar

Recomendado: Python 3.12 de 64 bits, con Tkinter. Instala las dependencias desde esta carpeta:

```sh
python -m pip install -r requirements.txt
python poka_yoke_mejorado5.py
```

En Windows puedes usar `py -3` en lugar de `python`.

## Vista previa de cámara

La cámara se abre automáticamente al iniciar, aunque no haya un modelo registrado. La vista previa no inicia la inspección ni incrementa contadores.

El botón **Abrir / reconectar cámara** está visible en la parte superior. Si necesitas otro dispositivo, abre un modelo en Administración, selecciona su número de cámara y pulsa **Abrir cámara**. Al cargar un modelo también se abre su cámara automáticamente.

En Windows, si un controlador abre el dispositivo pero no entrega imágenes, se intentan otras opciones de captura. Se espera el primer cuadro válido antes de mostrar la cámara como lista. El estado inferior informa el resultado; un fallo de lectura permite reconectar sin cerrar el programa.

Para actualizar desde 5.0, cierra la aplicación y reemplaza únicamente `poka_yoke_mejorado5.py`, conservando tu carpeta `datos`.

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

Esta versión de archivo único pasó 51 pruebas automatizadas, incluidas pruebas de interfaz con cámara simulada y del motor OpenCV sobre imágenes sintéticas. No se verificó una cámara física ni su controlador de Windows.

Comprueba en tu equipo enfoque, iluminación, resolución, velocidad y resultados con piezas reales. Las pruebas iniciales no garantizan una tasa de detección para todos los defectos. La presencia y el retiro se estiman visualmente; no hay un sensor físico de presencia. El motor indica diferencias por zona, sin diagnosticar automáticamente el tipo de defecto.

## Ejecutar las pruebas

```sh
python -m unittest discover -v
```

Las pruebas de interfaz necesitan una sesión gráfica. Sin ella se marcan omitidas; no se consideran ejecutadas. Las pruebas usan cámaras simuladas y no activan tu cámara física.
