from sqlalchemy import String, BigInteger, Integer
from sqlalchemy.orm import Mapped, mapped_column, DeclarativeBase, relationship

from vintasend_sqlalchemy.model_factory import GenericNotification, NotificationMeta


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column("id", BigInteger().with_variant(Integer, "sqlite"), primary_key=True)
    email: Mapped[str] = mapped_column("email", String(255), nullable=False)



class Notification(
    GenericNotification[User, int], 
    metaclass=NotificationMeta, 
    user_model=User, 
    user_primary_key_field_name="id", 
    user_primary_key_field_type=int
):
    def get_user_email(self) -> str:
        return self.get_user().email