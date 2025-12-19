import logging
import os
import io
import time
from dotenv import load_dotenv
from telegram import Update, constants, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler
from openai import OpenAI
from pydub import AudioSegment
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import Paragraph, Spacer
from reportlab.platypus import SimpleDocTemplate # Necesario para crear PDFs multi-página

# 1. Configuración de Log y Carga de Entorno
# ----------------------------------------------------------------------------------
load_dotenv()
# La clave de OpenAI se carga automáticamente del archivo .env
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

# **IMPORTANTE:** Token VÁLIDO. Si da InvalidToken, revocar y actualizar este.
BOT_TOKEN = os.getenv("BOT_TOKEN") # Tu token de Telegram

# Inicializar clientes
client = OpenAI(
    api_key=OPENAI_API_KEY,
    timeout=60
)

# Configuración básica de logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# --- 2. Funciones Auxiliares para PDF ---
# ----------------------------------------------------------------------------------

def create_pdf(summary, full_transcript, user_id):
    """Crea un archivo PDF con el resumen y la transcripción completa."""
    pdf_filename = f"resumen_audio_{user_id}_{int(time.time())}.pdf"
    
    # Crear un buffer en memoria para el PDF
    buffer = io.BytesIO()
    
    # Estilos de ReportLab
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name='NormalSpanish', parent=styles['Normal'], fontName='Helvetica', fontSize=10, leading=12))
    styles.add(ParagraphStyle(name='TitleStyle', parent=styles['Heading1'], fontName='Helvetica-Bold', fontSize=16, leading=18, spaceAfter=12))
    styles.add(ParagraphStyle(name='CustomHeading2', parent=styles['Heading2'], fontName='Helvetica-Bold', fontSize=12, leading=14, spaceAfter=8))
    
    story = []

    # Título
    story.append(Paragraph("Resumen y Transcripción de Audio por TranscriptorAudioIA Yeshúa la toraviviente.", styles['TitleStyle']))
    story.append(Spacer(1, 12))

    # Resumen
    story.append(Paragraph("<b>1. Resumen</b>", styles['CustomHeading2']))
    summary_paragraphs = summary.split('\n')
    for p in summary_paragraphs:
        if p.strip():
            story.append(Paragraph(p.strip(), styles['NormalSpanish']))
            story.append(Spacer(1, 6))

    story.append(Spacer(1, 24))

    # Transcripción
    story.append(Paragraph("<b>2. Transcripción Completa</b>", styles['CustomHeading2']))
    story.append(Spacer(1, 12))
    story.append(Paragraph(full_transcript, styles['NormalSpanish']))

    # Construir el PDF
    try:
        doc = SimpleDocTemplate(buffer, pagesize=letter, leftMargin=36, rightMargin=36, topMargin=36, bottomMargin=36)
        doc.build(story)

    except Exception as e:
        logger.error(f"Error al construir el PDF: {e}")
        return None, None

    # Mover el puntero al inicio del buffer y devolverlo
    buffer.seek(0)
    return buffer, pdf_filename

# --- 3. Funciones de Lógica de Negocio (IA y Audio) ---
# ----------------------------------------------------------------------------------

async def process_audio_and_summarize(update: Update, context: ContextTypes.DEFAULT_TYPE, audio_file_id: str, audio_type: str = "VOZ"):
    """Descarga, segmenta, transcribe, resume y genera el PDF."""
    chat_id = update.effective_chat.id
    
    # 1. Notificar inicio
    message = await context.bot.send_message(
        chat_id=chat_id,
        text="⏳ Descargando audio y preparando para segmentación..."
    )

    try:
        # 2. Descargar el archivo de audio de forma robusta
        # Esto resuelve los problemas de "Invalid data found" (Error de Decodificación OGG)
        file_object = await context.bot.get_file(audio_file_id)

        # Descargar el archivo a un buffer en memoria
        audio_file_in_memory = io.BytesIO()
        try:
            await file_object.download_to_memory(audio_file_in_memory)
        except Exception as e:
            err_text = str(e).lower()
            # Manejar caso conocido donde Telegram/descarga rechaza archivos muy grandes
            if "file is too big" in err_text or "file is too large" in err_text or "too large" in err_text:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=(
                        "⏱️ **Archivo demasiado grande para descargar**\n\n"
                        "Parece que el archivo supera el límite que puedo descargar directamente.\n\n"
                        "📌 Por favor divide el audio en partes de máximo 30–40 minutos usando este enlace:\n"
                        "https://audiotrimmer.com/\n\n"
                        "🔁 Una vez lo hayas cortado, envíame las partes y las procesaré sin error."
                    )
                )
                return
            # Si es otro error, relanzarlo para que caiga en el manejador general
            raise

        # Reiniciar el puntero para que AudioSegment pueda leer el archivo completo
        audio_file_in_memory.seek(0)
        
        # 3. Cargar el Audio para su procesamiento
        # AudioSegment.from_file requiere que el objeto sea un BytesIO si está en memoria
        audio = AudioSegment.from_file(audio_file_in_memory)

        # --- DETECCIÓN SIMPLE DE POSIBLE CANTO ---
        if audio.channels > 1:
            await context.bot.send_message(
                chat_id=chat_id,
                text=(
                    "🎵 **Posible cántico o música detectada**\n\n"
                    "⚠️ La transcripción de cantos puede contener errores.\n"
                    "✅ Audios hablados se transcriben con mayor precisión.\n\n"
                    "Si es un cántico, puedes continuar sabiendo esto."
                ),
                parse_mode=constants.ParseMode.MARKDOWN
            )
        
        duration_ms = len(audio)
        # Aviso para audios muy largos
        duration_minutes = duration_ms / 60000

        if duration_minutes > 50:
            await context.bot.send_message(
                chat_id=chat_id,
                text=(
                    "⏱️ **Audio muy largo detectado**\n\n"
                    "📌 Para mayor estabilidad, por favor divide el audio en partes.\n\n"
                    "🔗 Herramienta recomendada:\n"
                    "https://audiotrimmer.com/\n\n"
                    "✂️ Divide en partes de máximo 30–40 minutos y vuelve a enviarlas.\n\n"
                    "Cuando lo hayas cortado, envíame las partes y las procesaré sin dar error."
                )
            )
            # Detener procesamiento para evitar errores por audios demasiado largos
            return
        segment_duration_ms = 20 * 60 * 1000  # 20 minutos en milisegundos (límite de Whisper)
        
        segments = [
            audio[i:i + segment_duration_ms]
            for i in range(0, duration_ms, segment_duration_ms)
        ]
        
        full_transcript = ""
        
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=message.message_id,
            text=f"⚙️ Audio segmentado en {len(segments)} partes. Iniciando transcripción con Whisper..."
        )
        
        # 4. Transcripción de Segmentos
        for i, segment in enumerate(segments):
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message.message_id,
                text=f"🎤 Transcribiendo segmento {i+1} de {len(segments)}..."
            )
            
            # Exportar segmento a BytesIO como mp3 para la API de OpenAI
            segment_io = io.BytesIO()
            segment.export(segment_io, format="mp3")
            segment_io.seek(0)
            segment_io.name = "audio.mp3"
            
            # Llamada a la API de Whisper
            transcription = client.audio.transcriptions.create(
                model="whisper-1",
                file=segment_io,
                language="es" # Asumimos español, se puede mejorar
            )
            full_transcript += transcription.text + " "

        # Si el audio es CANTO, solo transcribimos y permitimos edición en el bot (no generamos PDF)
        if audio_type.upper() == "CANTO":
            # Guardar la transcripción y ofrecer opciones (Editar / Guardar) mediante botones
            context.user_data["last_transcription"] = full_transcript
            context.user_data["awaiting_correction"] = False

            keyboard = [
                [InlineKeyboardButton("✏️ Editar", callback_data="EDIT_CANTO")],
                [InlineKeyboardButton("💾 Guardar", callback_data="SAVE_CANTO")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)

            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message.message_id,
                text="✅ Transcripción completada (modo CANTO). Revisa la transcripción y elige una acción:",
            )

            # Enviar la transcripción en un mensaje separado y adjuntar los botones en otro mensaje
            await context.bot.send_message(chat_id=chat_id, text=full_transcript)
            await context.bot.send_message(chat_id=chat_id, text="Elige una opción:", reply_markup=reply_markup)

            return

        # 5. GENERACIÓN DE RESUMEN (OPTIMIZADO PARA COSTO)
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=message.message_id,
            text="🧠 Generando resumen optimizado…"
        )

        # Recorte inteligente (máx 8.000 caracteres)
        MAX_SUMMARY_CHARS = 8000
        summary_input = full_transcript[:MAX_SUMMARY_CHARS]

        summary_prompt = (
            "Resume el siguiente contenido en español en un máximo de TRES párrafos claros y concisos. "
            "Extrae únicamente las ideas principales, conclusiones y temas relevantes. "
            "No agregues información externa ni títulos.\n\n"
            "---\n\n" + summary_input
        )

        summary_response = client.chat.completions.create(
            model="gpt-4.1-mini",
            messages=[
                {"role": "system", "content": "Eres un asistente experto en resumir textos extensos."},
                {"role": "user", "content": summary_prompt}
            ],
            temperature=0.2
        )

        summary = summary_response.choices[0].message.content

        # 6. Generación de PDF y Envío (solo VOZ)
        pdf_data, pdf_filename = create_pdf(summary, full_transcript, chat_id)
        
        if pdf_data:
            # Envío del resumen de texto
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message.message_id,
                text="✅ **RESUMEN GENERADO CON ÉXITO**\n\n" + summary,
                parse_mode=constants.ParseMode.MARKDOWN
            )

            # Envío del archivo PDF
            await context.bot.send_document(
                chat_id=chat_id,
                document=pdf_data,
                filename=pdf_filename,
                caption="📄 Aquí tienes el archivo PDF con el resumen completo y la transcripción."
            )
        else:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message.message_id,
                text="❌ Error al generar el archivo PDF."
            )

    except Exception as e:
        logger.error(f"Error general en el procesamiento: {e}")
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=message.message_id,
            text=f"❌ Ocurrió un error inesperado al procesar el audio. Por favor, inténtalo de nuevo. Error: {e}"
        )
    
# --- 4. Manejadores de Telegram ---
# ----------------------------------------------------------------------------------

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Maneja el comando /start"""
    user = update.effective_user
    await update.message.reply_html(
        f"¡Hola, {user.first_name} 👋!\n\n"
        "Soy tu bot transcriptor y resumidor de audios largos.\n\n"
        "**Para iniciar**, simplemente **reenvía o sube** un mensaje de voz o un archivo de audio (MP3, OGG, M4A, etc.) de Telegram. Yo me encargaré del resumen de 3 páginas."
    )

async def echo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Responde a cualquier mensaje de texto simple."""
    await update.message.reply_text(
        "Por favor, envíame o reenvíame un **mensaje de voz o un archivo de audio**. "
        "No puedo procesar mensajes de texto. 😉"
    )


async def audio_type_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("awaiting_type"):
        return

    choice = update.message.text.strip().upper()
    audio_file_id = context.user_data.get("audio_file_id")

    if choice not in ["VOZ", "CANTO"]:
        await update.message.reply_text("❌ Responde solo con: VOZ o CANTO")
        return

    context.user_data["awaiting_type"] = False

    # Determinar tipo y lanzar el procesamiento con el tipo adecuado
    audio_type = "CANTO" if choice == "CANTO" else "VOZ"

    if choice == "CANTO":
        await update.message.reply_text(
            "🎵 **Modo CÁNTICO activado**\n\n"
            "⚠️ Puede haber errores en la letra.\n"
            "✏️ Recomendado solo para referencia."
        )

    await update.message.reply_text("⏳ Iniciando transcripción…")

    context.application.create_task(
        process_audio_and_summarize(update, context, audio_file_id, audio_type=audio_type)
    )


async def correction_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Maneja correcciones de transcripciones en modo CANTO."""
    # If we're not expecting a free-text correction, ignore
    if not context.user_data.get("awaiting_correction"):
        return

    corrected = update.message.text.strip()
    # Guardar la versión final
    context.user_data["awaiting_correction"] = False
    context.user_data["final_transcription"] = corrected

    await update.message.reply_text(
        "✅ Transcripción actualizada y guardada. Aquí está la versión final:\n\n" + corrected
    )


async def canto_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Maneja los callbacks de los botones Editar/Guardar para CANTO."""
    query = update.callback_query
    if not query:
        return
    await query.answer()

    data = query.data
    chat_id = query.message.chat_id

    if data == "EDIT_CANTO":
        last = context.user_data.get("last_transcription", "")
        context.user_data["awaiting_correction"] = True
        # Enviar la transcripción actual y pedir la versión corregida
        await query.message.reply_text("✏️ Envíame la transcripción corregida. Actualmente:\n\n" + last)

    elif data == "SAVE_CANTO":
        # Guardar la transcripción tal cual está en last_transcription
        final = context.user_data.get("last_transcription", "")
        context.user_data["awaiting_correction"] = False
        context.user_data["final_transcription"] = final
        await query.message.reply_text("💾 Transcripción guardada. Aquí está la versión final:\n\n" + final)

async def handle_audio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Función que se llama cuando el bot recibe un mensaje de audio o voz."""
    
    # 1. Notificar al usuario inmediatamente
    await update.message.reply_text(
        "🎧 **Audio recibido**\n\n"
        "Responde con una opción:\n\n"
        "🎙️ Escribe: VOZ  → si es audio hablado\n"
        "🎵 Escribe: CANTO → si es un cántico o canción\n\n"
        "⏳ El procesamiento iniciará según tu elección."
    )

    # 2. Obtener el File ID del audio
    if update.message.voice:
        # Mensaje de voz de Telegram (tipo OGG)
        audio_file_id = update.message.voice.file_id
    elif update.message.audio:
        # Archivo de audio adjunto (MP3, M4A, etc.)
        audio_file_id = update.message.audio.file_id
    else:
        # En teoría, no debería llegar aquí si el filtro funciona
        return

    # Guardar elección pendiente y el file id en user_data
    context.user_data["audio_file_id"] = audio_file_id
    context.user_data["awaiting_type"] = True

    logger.info(f"Procesando audio ID: {audio_file_id} para el chat: {update.effective_chat.id}")

    # 3. Esperar a la elección del usuario; cuando responda, `audio_type_handler` iniciará el procesamiento


# --- 5. Función Principal de Ejecución ---
# ----------------------------------------------------------------------------------

def main() -> None:
    """Inicia el bot."""
    # Crea la aplicación y pásale el token
    application = (
    Application.builder()
    .token(BOT_TOKEN)
    .connect_timeout(60)
    .read_timeout(60)
    .build()
    )

    # Registra los manejadores (handlers)
    application.add_handler(CommandHandler("start", start_command))
    # Handler para recibir la elección VOZ / CANTO (se añade antes del echo genérico)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, audio_type_handler))
    # Handler para callbacks de botones (Editar / Guardar) en modo CANTO
    application.add_handler(CallbackQueryHandler(canto_callback_handler))
    # Handler para recibir correcciones en modo CANTO (texto libre enviado por el usuario)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, correction_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo))
    
    # Maneja mensajes de audio y voz. ¡ESTA LÍNEA ES VITAL!
    application.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_audio))

    # Inicia el bot (se ejecuta hasta que presionas Ctrl+C)
    logger.info("El bot ha iniciado. Esperando mensajes...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()