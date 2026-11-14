from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.core.security import validate_new_password


class PasswordChangeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=8, max_length=128)
    confirm_password: str = Field(min_length=8, max_length=128)

    _password_policy = field_validator("new_password")(validate_new_password)

    @model_validator(mode="after")
    def passwords_match(self):
        if self.new_password != self.confirm_password:
            raise ValueError("Passwords do not match.")
        if self.current_password == self.new_password:
            raise ValueError("Choose a password different from your current password.")
        return self
