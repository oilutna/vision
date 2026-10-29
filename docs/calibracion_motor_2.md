# Corrección de calibración · motor 2

Se conserva el programa y su interfaz. La referencia visible sigue siendo una fotografía real representativa, sin fusionar versiones.

La búsqueda y el ajuste fino se realizan contra la geometría nativa de cada referencia; después se aplica su transformación conocida a las coordenadas de zonas. Así una pose aceptada en una referencia no se rechaza por aplicar el límite de movimiento fino a la transformación completa desde la primera foto.

Se estima variación de captura dejando cada OK fuera de sus candidatos de comparación. Solo se admiten vecinos completos próximos: todas las zonas deben quedar dentro de un máximo del 1% de área de defecto para contribuir a calibración. Esa variación enseña límites de área por zona y referencia, acotados al 1%; no modifica el umbral por píxel ni fusiona patrones. Los factores y la tolerancia existentes siguen aplicándose. Las NG no se utilizan para entrenar.

Verificación local sobre el modelo guardado por el usuario: 10/10 referencias OK aprobadas y 10/10 fotos etiquetadas NG rechazadas. La OK 7 pasó de sin evaluación a OK después de corregir la geometría. Son comprobaciones sobre datos de preparación; no una validación independiente ni una garantía de desempeño en vivo. No se publicaron fotografías ni configuraciones personales.

10 regresiones enfocadas aprobadas; la compatibilidad con pruebas anteriores se recoge en el informe de ejecución local. El almacenamiento no fue modificado y mantiene sus advertencias de conexiones abiertas de la revisión previa.

El criterio indicado por el usuario es quitarse los lentes o abrir la boca como NG. Las fotos etiquetadas se comprobaron como conjunto; falta verificar por separado cada condición y nuevas fotos OK, con lentes puestos y boca cerrada, además del flujo de cámara real. Los cambios no rígidos de un rostro no representan la repetibilidad de un ensamble industrial.

Para usar: cierre la aplicación guardando los ajustes pendientes, respalde el Python anterior y sustitúyalo por el corregido en la misma carpeta. Conserve `datos`. Repita Comprobar 10 OK + 10 NG y habilite el modelo. No se cierra ni reinicia automáticamente una sesión de edición abierta.

La precisión industrial sigue requiriendo piezas físicas reales y validación independiente. Una calibración del rostro no debe reutilizarse como receta de una pieza industrial.
