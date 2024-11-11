from sqlalchemy import String, BigInteger, Integer
from sqlalchemy.orm import Mapped, mapped_column, declarative_base

from vintasend_sqlalchemy.model_factory import create_notification_model


Base = declarative_base()
metadata = Base.metadata


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column("id", BigInteger().with_variant(Integer, "sqlite"), primary_key=True)
    email: Mapped[str] = mapped_column("email", String(255), nullable=False)


BaseNotification = create_notification_model(User, "id", BigInteger)


class Notification(BaseNotification):
    def get_user_email(self) -> str:
        return self.get_user().email