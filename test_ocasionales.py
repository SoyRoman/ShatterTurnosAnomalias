"""
Pruebas del calculo de servicios ocasionales. No necesitan base de datos.

    python test_ocasionales.py
"""
import datetime as dt
import sys

import ocasionales as oc

fallas = []


def comprobar(nombre, obtenido, esperado):
    ok = obtenido == esperado
    print(f"[{'OK ' if ok else 'MAL'}] {nombre}" + ("" if ok else f": {obtenido!r} (esperado {esperado!r})"))
    if not ok:
        fallas.append(nombre)


D = dt.date
T = dt.time

# Festivos oficiales de Colombia en 2026 (calendario publicado)
oficiales_2026 = {
    D(2026, 1, 1), D(2026, 1, 12), D(2026, 3, 23), D(2026, 4, 2), D(2026, 4, 3),
    D(2026, 5, 1), D(2026, 5, 18), D(2026, 6, 8), D(2026, 6, 15), D(2026, 6, 29),
    D(2026, 7, 20), D(2026, 8, 7), D(2026, 8, 17), D(2026, 10, 12), D(2026, 11, 2),
    D(2026, 11, 16), D(2026, 12, 8), D(2026, 12, 25),
}
comprobar("los 18 festivos de 2026", oc.festivos(2026), oficiales_2026)
comprobar("domingo cuenta como festivo", oc.es_festivo(D(2026, 10, 18)), True)
comprobar("martes normal no es festivo", oc.es_festivo(D(2026, 10, 13)), False)

comprobar("detecta 'Ocasional' en el puesto", oc.es_ocasional("Ocasional Martín Virrería Salomia"), True)
comprobar("sin importar mayusculas ni tildes", oc.es_ocasional("13.OCASIONAL CALI - OCASIONAL CALI"), True)
comprobar("un puesto normal no es ocasional", oc.es_ocasional("N.U PORTERÍA - CELULAR"), False)

comprobar("corte de octubre", oc.rango_corte("2026-10"), (D(2026, 9, 21), D(2026, 10, 20)))
comprobar("corte de enero cruza el año", oc.rango_corte("2027-01"), (D(2026, 12, 21), D(2027, 1, 20)))
comprobar("el 20 es del corte del mes", oc.corte_de(D(2026, 10, 20)), "2026-10")
comprobar("el 21 es del corte siguiente", oc.corte_de(D(2026, 10, 21)), "2026-11")
comprobar("el 21 de diciembre es del corte de enero", oc.corte_de(D(2026, 12, 21)), "2027-01")

# 2026-10-13 es martes; 2026-10-17 sabado; 2026-10-18 domingo
comprobar("E 06-18 en dia habil", oc.separar_horas(D(2026, 10, 13), T(6), T(18)),
          {'diurna': 12.0, 'nocturna': 0.0, 'diurna_festiva': 0.0, 'nocturna_festiva': 0.0})
comprobar("F 18-06 en dia habil: 1 diurna + 11 nocturnas", oc.separar_horas(D(2026, 10, 13), T(18), T(6)),
          {'diurna': 1.0, 'nocturna': 11.0, 'diurna_festiva': 0.0, 'nocturna_festiva': 0.0})
comprobar("F del sabado: lo que pasa de medianoche es dominical",
          oc.separar_horas(D(2026, 10, 17), T(18), T(6)),
          {'diurna': 1.0, 'nocturna': 5.0, 'diurna_festiva': 0.0, 'nocturna_festiva': 6.0})
comprobar("E del domingo: todo diurna festiva", oc.separar_horas(D(2026, 10, 18), T(6), T(18)),
          {'diurna': 0.0, 'nocturna': 0.0, 'diurna_festiva': 12.0, 'nocturna_festiva': 0.0})
comprobar("festivo del 12 de octubre (lunes)", oc.separar_horas(D(2026, 10, 12), T(14), T(22)),
          {'diurna': 0.0, 'nocturna': 0.0, 'diurna_festiva': 5.0, 'nocturna_festiva': 3.0})
comprobar("turno sin horario (ausencia) no suma", oc.separar_horas(D(2026, 10, 13), None, None),
          {'diurna': 0.0, 'nocturna': 0.0, 'diurna_festiva': 0.0, 'nocturna_festiva': 0.0})
comprobar("turno de 24 h suma 24", sum(oc.separar_horas(D(2026, 10, 13), T(7), T(7)).values()), 24.0)

print("\nRESULTADO:", "todas las pruebas pasaron" if not fallas else f"FALLARON {len(fallas)}")
sys.exit(1 if fallas else 0)
