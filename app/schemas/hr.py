from datetime import datetime
from typing import Optional
from pydantic import BaseModel, model_validator
from app.schemas.common import StrictModel


# --- Onboarding tasks ------------------------------------------------------
class OnboardingTaskCreate(StrictModel):
    employee_id: int
    task_type: str
    notes: Optional[str] = None


class OnboardingTaskUpdate(StrictModel):
    status: Optional[str] = None
    notes: Optional[str] = None
    completed_by: Optional[str] = None
    signed: Optional[bool] = None
    document_id: Optional[int] = None


class OnboardingTaskResponse(BaseModel):
    # None on a task of the default checklist before it is saved (#662)
    id: Optional[int] = None
    employee_id: int
    task_type: str
    status: str
    signed: bool = False
    signed_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    completed_by: Optional[str] = None
    document_id: Optional[int] = None
    notes: Optional[str] = None
    model_config = {"from_attributes": True}


class OnboardingChecklistResponse(BaseModel):
    employee_id: int
    employee_name: Optional[str] = None
    complete: int = 0
    total: int = 0
    percent_complete: float = 0
    tasks: list[OnboardingTaskResponse] = []


# --- Employee document vault ----------------------------------------------
class EmployeeDocumentResponse(BaseModel):
    id: int
    employee_id: Optional[int] = None
    filename: str
    doc_category: Optional[str] = None
    mime_type: Optional[str] = None
    file_size: Optional[int] = None
    uploaded_at: Optional[datetime] = None
    # Copied in by the upgrade to 2.18.0 from the folder every company used
    # to share (it may be another company's), or not there to copy.
    from_shared_folder: bool = False
    missing: bool = False
    model_config = {"from_attributes": True}

    @model_validator(mode="after")
    def _no_size_without_bytes(self):
        # the size the row kept is of a file that isn't here
        if self.missing:
            self.file_size = None
        return self
