import psycopg2
import psycopg2.extras
import os
import requests
from datetime import datetime, timedelta
from collections import defaultdict

DATABASE_URL = os.environ["DATABASE_URL"]
RESEND_API_KEY = os.environ["RESEND_API_KEY"]
REPORT_EMAIL = "meli_que@yahoo.com"
HORA_ENTRADA = 8  # 8:00am

def get_week_records():
    """Obtiene todos los registros de la semana pasada (lunes a domingo)."""
    today = datetime.utcnow().date()
    last_monday = today - timedelta(days=today.weekday() + 7)
    last_sunday = last_monday + timedelta(days=6)

    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT r.full_name, r.event_type, r.timestamp, r.latitude, r.longitude
        FROM records r
        WHERE r.timestamp::date BETWEEN %s AND %s
        ORDER BY r.full_name, r.timestamp
    """, (last_monday, last_sunday))
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows, last_monday, last_sunday

def build_html_report(records, week_start, week_end):
    """Construye el reporte HTML."""

    # Agrupar por empleado y por día
    # estructura: data[nombre][fecha] = {"in": hora, "out": hora}
    data = defaultdict(lambda: defaultdict(dict))

    for r in records:
        ts = r["timestamp"]
        # Convertir a hora local Costa Rica (UTC-6)
        local_ts = ts - timedelta(hours=6)
        nombre = r["full_name"]
        fecha = local_ts.date()
        tipo = r["event_type"]

        if tipo == "in":
            # Guardar la primera entrada del día
            if "in" not in data[nombre][fecha]:
                data[nombre][fecha]["in"] = local_ts
        elif tipo == "out":
            # Guardar la última salida del día
            data[nombre][fecha]["out"] = local_ts

    # Generar días de la semana (lun-vie)
    dias = []
    for i in range(7):
        d = week_start + timedelta(days=i)
        dias.append(d)

    # Construir tabla HTML por empleado
    empleados_html = ""
    resumen_tardanzas = []

    for nombre in sorted(data.keys()):
        filas = ""
        tiene_tardanza = False

        for dia in dias:
            dia_data = data[nombre].get(dia, {})
            hora_in = dia_data.get("in")
            hora_out = dia_data.get("out")

            dia_str = dia.strftime("%A %d/%m").capitalize()

            if hora_in:
                hora_in_str = hora_in.strftime("%H:%M")
                tardo = hora_in.hour > HORA_ENTRADA or (hora_in.hour == HORA_ENTRADA and hora_in.minute > 0)
                hora_out_str = hora_out.strftime("%H:%M") if hora_out else "—"

                if tardo:
                    tiene_tardanza = True
                    resumen_tardanzas.append({
                        "nombre": nombre,
                        "dia": dia_str,
                        "entrada": hora_in_str,
                        "salida": hora_out_str
                    })
                    filas += f"""
                    <tr style="background-color:#fff3f3;">
                        <td style="padding:8px 12px;border-bottom:1px solid #eee;">{dia_str}</td>
                        <td style="padding:8px 12px;border-bottom:1px solid #eee;color:#e53935;font-weight:bold;">
                            ⚠️ {hora_in_str}
                        </td>
                        <td style="padding:8px 12px;border-bottom:1px solid #eee;">{hora_out_str}</td>
                        <td style="padding:8px 12px;border-bottom:1px solid #eee;color:#e53935;font-size:12px;">Tardanza</td>
                    </tr>"""
                else:
                    filas += f"""
                    <tr>
                        <td style="padding:8px 12px;border-bottom:1px solid #eee;">{dia_str}</td>
                        <td style="padding:8px 12px;border-bottom:1px solid #eee;color:#2e7d32;">{hora_in_str}</td>
                        <td style="padding:8px 12px;border-bottom:1px solid #eee;">{hora_out_str}</td>
                        <td style="padding:8px 12px;border-bottom:1px solid #eee;color:#2e7d32;font-size:12px;">✓ A tiempo</td>
                    </tr>"""
            else:
                filas += f"""
                <tr style="background-color:#fafafa;">
                    <td style="padding:8px 12px;border-bottom:1px solid #eee;color:#aaa;">{dia_str}</td>
                    <td style="padding:8px 12px;border-bottom:1px solid #eee;color:#aaa;">—</td>
                    <td style="padding:8px 12px;border-bottom:1px solid #eee;color:#aaa;">—</td>
                    <td style="padding:8px 12px;border-bottom:1px solid #eee;color:#aaa;font-size:12px;">Sin registro</td>
                </tr>"""

        header_color = "#ffebee" if tiene_tardanza else "#e8f5e9"
        header_text_color = "#c62828" if tiene_tardanza else "#1b5e20"

        empleados_html += f"""
        <div style="margin-bottom:32px;">
            <div style="background:{header_color};padding:12px 16px;border-radius:8px 8px 0 0;border-left:4px solid {'#e53935' if tiene_tardanza else '#2e7d32'};">
                <strong style="color:{header_text_color};font-size:15px;">👤 {nombre}</strong>
                {'<span style="float:right;color:#e53935;font-size:12px;">⚠️ Tiene tardanzas esta semana</span>' if tiene_tardanza else '<span style="float:right;color:#2e7d32;font-size:12px;">✓ Sin tardanzas</span>'}
            </div>
            <table style="width:100%;border-collapse:collapse;border:1px solid #eee;border-top:none;">
                <thead>
                    <tr style="background:#f5f5f5;">
                        <th style="padding:8px 12px;text-align:left;font-size:12px;color:#666;">DÍA</th>
                        <th style="padding:8px 12px;text-align:left;font-size:12px;color:#666;">ENTRADA</th>
                        <th style="padding:8px 12px;text-align:left;font-size:12px;color:#666;">SALIDA</th>
                        <th style="padding:8px 12px;text-align:left;font-size:12px;color:#666;">ESTADO</th>
                    </tr>
                </thead>
                <tbody>{filas}</tbody>
            </table>
        </div>"""

    # Resumen de tardanzas al inicio
    resumen_html = ""
    if resumen_tardanzas:
        filas_resumen = ""
        for t in resumen_tardanzas:
            filas_resumen += f"""
            <tr>
                <td style="padding:8px 12px;border-bottom:1px solid #eee;">{t['nombre']}</td>
                <td style="padding:8px 12px;border-bottom:1px solid #eee;">{t['dia']}</td>
                <td style="padding:8px 12px;border-bottom:1px solid #eee;color:#e53935;font-weight:bold;">{t['entrada']}</td>
                <td style="padding:8px 12px;border-bottom:1px solid #eee;">{t['salida']}</td>
            </tr>"""

        resumen_html = f"""
        <div style="margin-bottom:32px;background:#fff3e0;border:1px solid #ffcc02;border-radius:8px;padding:20px;">
            <h3 style="margin:0 0 16px;color:#e65100;">⚠️ Resumen de tardanzas de la semana</h3>
            <table style="width:100%;border-collapse:collapse;">
                <thead>
                    <tr style="background:#ffe0b2;">
                        <th style="padding:8px 12px;text-align:left;font-size:12px;color:#bf360c;">EMPLEADO</th>
                        <th style="padding:8px 12px;text-align:left;font-size:12px;color:#bf360c;">DÍA</th>
                        <th style="padding:8px 12px;text-align:left;font-size:12px;color:#bf360c;">LLEGÓ A LAS</th>
                        <th style="padding:8px 12px;text-align:left;font-size:12px;color:#bf360c;">SALIÓ A LAS</th>
                    </tr>
                </thead>
                <tbody>{filas_resumen}</tbody>
            </table>
        </div>"""
    else:
        resumen_html = """
        <div style="margin-bottom:32px;background:#e8f5e9;border:1px solid #a5d6a7;border-radius:8px;padding:20px;text-align:center;">
            <p style="margin:0;color:#2e7d32;font-size:16px;">✅ ¡Excelente semana! Todos llegaron a tiempo.</p>
        </div>"""

    semana_str = f"{week_start.strftime('%d/%m/%Y')} — {week_end.strftime('%d/%m/%Y')}"

    html = f"""
    <!DOCTYPE html>
    <html>
    <head><meta charset="UTF-8"></head>
    <body style="font-family:Arial,sans-serif;max-width:700px;margin:0 auto;padding:20px;color:#333;">

        <div style="background:#0a0a0f;padding:24px;border-radius:12px;margin-bottom:32px;text-align:center;">
            <h1 style="margin:0;color:#7fff6e;font-size:28px;">CheckIN</h1>
            <p style="margin:8px 0 0;color:#5a5a7a;font-size:14px;">Reporte semanal de asistencia</p>
            <p style="margin:4px 0 0;color:#e8e8f0;font-size:13px;">{semana_str}</p>
        </div>

        {resumen_html}

        <h2 style="color:#333;font-size:16px;margin-bottom:20px;">Detalle por empleado</h2>

        {empleados_html}

        <div style="margin-top:32px;padding-top:16px;border-top:1px solid #eee;text-align:center;">
            <p style="color:#aaa;font-size:12px;">Este reporte fue generado automáticamente por CheckIN.<br>
            Hora de entrada oficial: 8:00 AM</p>
        </div>

    </body>
    </html>"""

    return html

def send_email(html, week_start, week_end):
    semana_str = f"{week_start.strftime('%d/%m')} al {week_end.strftime('%d/%m/%Y')}"
    response = requests.post(
        "https://api.resend.com/emails",
        headers={
            "Authorization": f"Bearer {RESEND_API_KEY}",
            "Content-Type": "application/json"
        },
        json={
            "from": "onboarding@resend.dev",
            "to": [REPORT_EMAIL],
            "subject": f"📋 Reporte de asistencia — Semana {semana_str}",
            "html": html
        }
    )
    print(f"Email enviado: {response.status_code}")
    print(response.json())
    return response.status_code == 200

if __name__ == "__main__":
    print("Obteniendo registros...")
    records, week_start, week_end = get_week_records()
    print(f"Registros encontrados: {len(records)}")
    print("Generando reporte...")
    html = build_html_report(records, week_start, week_end)
    print("Enviando email...")
    success = send_email(html, week_start, week_end)
    if success:
        print("✅ Reporte enviado exitosamente")
    else:
        print("❌ Error al enviar el reporte")
