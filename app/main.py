from fastapi import FastAPI, Request, HTTPException, Depends, Header
from sqlalchemy.orm import Session
from sqlalchemy.exc import SQLAlchemyError
from app.database.models import Message, MultimediaMessage, Order, OrderItem, FlowState, User, WindowQuote, Window
from app.database.connection import get_db
from app.services.alvochat_api import AlvoChatAPI
from app.flows import get_flow, get_all_flows
from app.utils.email_sender import send_email
from app.config import settings
import logging
from datetime import datetime
import json

app = FastAPI()
alvochat_api = AlvoChatAPI()
logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger.info("AlvoChatAPI instance created")

@app.get("/")
async def root():
    return {"message": "Bienvenido al sistema de presupuestos de ventanas"}

@app.post("/webhook")
async def webhook(
    request: Request,
    db: Session = Depends(get_db),
    authorization: str = Header(None)
):
    logger.debug(f"Received webhook request. Query params: {request.query_params}")
    logger.debug(f"Authorization header: {authorization}")

    # Check for token in query params
    token = request.query_params.get("token")

    # If not in query params, check in Authorization header
    if not token and authorization:
        if authorization.startswith("Bearer "):
            token = authorization.split("Bearer ")[1]
        else:
            token = authorization

    if not token:
        logger.warning("No token received")
        raise HTTPException(status_code=403, detail="No token provided")

    if token != settings.WEBHOOK_TOKEN:
        logger.warning(f"Invalid token received: {token}")
        raise HTTPException(status_code=403, detail="Invalid token")

    logger.info(f"Received webhook request with valid token")
    try:
        body = await request.json()
        logger.debug(f"Webhook request body: {json.dumps(body, indent=2)}")
        message_data = body.get("data", {})
        message_type = message_data.get("type")
        sender = message_data.get("from")
        text = message_data.get("body", "")
        wamid = message_data.get("id")

        if not sender or not message_type or not wamid:
            raise HTTPException(status_code=400, detail="Invalid message format")

        # Verificar si el mensaje ya existe
        existing_message = db.query(Message).filter(Message.wamid == wamid).first()
        if existing_message:
            logger.info(f"Duplicate message received with wamid: {wamid}")
            return {"status": "success", "message": "Duplicate message"}

        user = get_or_create_user(db, sender)
        flow_state = db.query(FlowState).filter(FlowState.user_id == sender).first()
        if not flow_state:
            # Si es el primer mensaje del usuario, creamos un nuevo FlowState con valores iniciales
            flow_state = FlowState(
                user_id=sender,
                current_flow="none",
                current_node=None,
                context={}
            )
            db.add(flow_state)
            db.commit()
        else:
            # Si el usuario ya existe pero no está en un flujo, aseguramos que el estado sea "none"
            if not flow_state.current_flow or flow_state.current_flow == "none":
                flow_state.current_flow = "none"
                flow_state.current_node = None
                flow_state.context = {}
            flow_state.last_updated = datetime.now()

        next_node = None
        response = ""
        flow = None

        if flow_state.current_flow == "none":
            next_node = None

        # Si el flujo actual es "none", intentamos identificar un nuevo flujo basado en el mensaje
        if flow_state.current_flow == "none":
            all_flows = get_all_flows()
            for flow in all_flows:
                if any(keyword in text.lower() for keyword in flow.keywords):
                    flow_state.current_flow = flow.name
                    flow_state.current_node = "start"
                    flow_state.context = {}
                    flow = get_flow(flow_state.current_flow)
                    next_node, response = flow.process_message("start", message_data, flow_state.context)
                    break
    
            if flow_state.current_flow == "none":
                next_node = None
                response = "Bienvenido. No estás en ningún flujo específico. ¿En qué puedo ayudarte?"
        elif flow_state.current_flow:
            flow = get_flow(flow_state.current_flow)
            if flow:
                next_node, response = flow.process_message(flow_state.current_node, message_data, flow_state.context)
            else:
                next_node = None
                response = "Lo siento, ha ocurrido un error al procesar tu mensaje."
                flow_state.current_flow = "none"
                flow_state.current_node = None
                flow_state.context = {}
        else:
            next_node = None
            response = "Lo siento, no pude procesar tu mensaje. ¿Puedes intentar de nuevo?"

        if next_node is not None:
            flow_state.current_node = next_node

            # Actualizar el contexto con la información de la ventana
            if flow_state.current_flow == "window_quote":
                if "current_window" not in flow_state.context:
                    flow_state.context["current_window"] = {}

                if flow_state.current_node == "ask_reference":
                    flow_state.context["current_window"]["reference"] = text
                elif flow_state.current_node == "ask_width":
                    flow_state.context["current_window"]["width"] = text
                elif flow_state.current_node == "ask_height":
                    flow_state.context["current_window"]["height"] = text
                elif flow_state.current_node == "ask_image":
                    if message_type == "image":
                        flow_state.context["current_window"]["image"] = message_data.get("image", {}).get("link")
                elif flow_state.current_node == "ask_color":
                    flow_state.context["current_window"]["color"] = message_data.get("interactive", {}).get("list_reply", {}).get("id")
                elif flow_state.current_node == "ask_blind":
                    flow_state.context["current_window"]["has_blind"] = message_data.get("interactive", {}).get("button_reply", {}).get("id") == "si_persiana"
                elif flow_state.current_node == "ask_motor":
                    flow_state.context["current_window"]["motorized_blind"] = message_data.get("interactive", {}).get("button_reply", {}).get("id") == "si_motor"
                elif flow_state.current_node == "ask_opening":
                    flow_state.context["current_window"]["opening_type"] = message_data.get("interactive", {}).get("list_reply", {}).get("id")
                elif flow_state.current_node == "ask_another":
                    if "windows" not in flow_state.context:
                        flow_state.context["windows"] = []
                    flow_state.context["windows"].append(flow_state.context.pop("current_window", {}))

        if next_node is None and flow_state.current_flow == "window_quote":
            # Generar y enviar el resumen del presupuesto
            summary = generate_quote_summary(flow_state.context)
            if user.email:
                send_email(user.email, "Resumen de presupuesto de ventanas", summary)
                response += "\n\nSe ha enviado un resumen detallado a tu correo electrónico."
            else:
                response += "\n\n" + summary
            flow_state.current_flow = "none"
            flow_state.current_node = None
            flow_state.context = {}

        # Guardar los cambios en la base de datos
        db.commit()

        # Guardar el mensaje en la base de datos
        new_message = Message(
            wamid=wamid,
            sender_id=sender,
            content=json.dumps(message_data),
            message_type=message_type,
            timestamp=datetime.now()
        )
        db.add(new_message)
        db.commit()

        # Guardar la respuesta del bot
        bot_response = Message(
            wamid=f"bot_response_{wamid}",
            sender_id="BOT",
            content=json.dumps({"text": response}),
            message_type="text",
            timestamp=datetime.now()
        )
        db.add(bot_response)
        db.commit()

        # Manejar diferentes tipos de mensajes
        if message_type in ["image", "video", "audio", "document"]:
            save_multimedia_message(db, new_message, message_data)
        elif message_type == "order":
            save_order(db, new_message, message_data)

        # Enviar la respuesta
        if response:
            logger.debug(f"Attempting to send message to {sender}: {response}")
            try:
                if isinstance(response, dict) and response.get("type") == "interactive":
                    result = alvochat_api.send_interactive_message(sender, response["interactive"])
                else:
                    result = alvochat_api.send_message(sender, response)
                logger.info(f"Message sent successfully: {result}")
            except Exception as e:
                logger.error(f"Error sending message: {str(e)}")

        return {"status": "success", "message": "Message processed"}

    except SQLAlchemyError as e:
        db.rollback()
        logger.error(f"Database error: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail="Database error")
    except Exception as e:
        logger.error(f"Error processing webhook: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

def get_or_create_user(db: Session, user_id: str) -> User:
    user = db.query(User).filter(User.user_id == user_id).first()
    if not user:
        user = User(user_id=user_id)
        db.add(user)
        db.commit()
    return user

def generate_quote_summary(context: dict) -> str:
    windows = context.get("windows", [])
    summary = "Resumen de presupuesto de ventanas:\n\n"
    for i, window in enumerate(windows, 1):
        summary += f"Ventana {i}:\n"
        summary += f"  Referencia: {window.get('reference', 'N/A')}\n"
        summary += f"  Ancho: {window.get('width', 'N/A')} cm\n"
        summary += f"  Alto: {window.get('height', 'N/A')} cm\n"
        summary += f"  Color: {window.get('color', 'N/A')}\n"
        summary += f"  Persiana: {'Sí' if window.get('has_blind') else 'No'}\n"
        if window.get('has_blind'):
            summary += f"  Motorizada: {'Sí' if window.get('motorized_blind') else 'No'}\n"
        summary += f"  Tipo de apertura: {window.get('opening_type', 'N/A')}\n"
        if window.get('image'):
            summary += f"  Imagen: {window.get('image')}\n"
        summary += "\n"
    return summary

def save_multimedia_message(db: Session, message: Message, message_data: dict):
    media_data = message_data.get("media", {})
    multimedia_message = MultimediaMessage(
        message_id=message.id,
        media_type=message.message_type,
        media_id=media_data.get("id"),
        media_url=media_data.get("link")
    )
    db.add(multimedia_message)
    db.commit()

def save_order(db: Session, message: Message, message_data: dict):
    order_data = message_data.get("order", {})
    new_order = Order(
        message_id=message.id,
        catalog_id=order_data.get("catalog_id"),
        status="recibido",
        total_price=order_data.get("total_amount", {}).get("amount")
    )
    db.add(new_order)
    for item in order_data.get("products", []):
        order_item = OrderItem(
            order=new_order,
            product_retailer_id=item.get("product_retailer_id"),
            catalog_id=item.get("catalog_id"),
            item_id=item.get("id"),
            quantity=item.get("quantity"),
            item_price=item.get("price"),
            currency=item.get("currency")
        )
        db.add(order_item)
    db.commit()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)

