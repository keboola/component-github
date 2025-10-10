import logging
from typing import List, Optional

from keboola.component.exceptions import UserException
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator


class Authorization(BaseModel):
    """GitHub authorization configuration"""
    github_token: str = Field(alias="#github_token")
    
    @field_validator("github_token")
    def token_must_not_be_empty(cls, v):
        if not v or not v.strip():
            raise UserException("GitHub token cannot be empty")
        return v.strip()


class Configuration(BaseModel):
    """Main configuration model for GitHub extractor"""
    authorization: Authorization
    endpoints: List[str]
    debug: bool = False
    
    # Optional row-level configuration (merged from configRowSchema)
    organization_name: Optional[str] = None
    repository_owner: Optional[str] = None
    repository_name: Optional[str] = None
    issue_state: str = "all"
    commits_since: Optional[str] = None

    def __init__(self, **data):
        try:
            super().__init__(**data)
        except ValidationError as e:
            error_messages = [f"{err['loc'][0]}: {err['msg']}" for err in e.errors()]
            raise UserException(f"Configuration Validation Error: {', '.join(error_messages)}")

        if self.debug:
            logging.getLogger().setLevel(logging.DEBUG)
            logging.debug("Component will run in Debug mode")
    
    @field_validator("endpoints")
    def endpoints_must_not_be_empty(cls, v):
        if not v or len(v) == 0:
            raise UserException("At least one endpoint must be selected")
        
        valid_endpoints = [
            "organizations",
            "organization_members",
            "organization_teams", 
            "organization_repositories",
            "repository_issues",
            "repository_commits"
        ]
        
        for endpoint in v:
            if endpoint not in valid_endpoints:
                raise UserException(f"Invalid endpoint: {endpoint}")
        
        return v
    
    @model_validator(mode='after')
    def validate_endpoint_requirements(self):
        """Validate that required parameters are provided for selected endpoints"""
        org_endpoints = ["organization_members", "organization_teams", "organization_repositories"]
        repo_endpoints = ["repository_issues", "repository_commits"]
        
        # Check if any organization endpoints are selected
        if any(ep in self.endpoints for ep in org_endpoints):
            if not self.organization_name:
                raise UserException(
                    f"Organization name is required when using endpoints: {', '.join(org_endpoints)}"
                )
        
        # Check if any repository endpoints are selected
        if any(ep in self.endpoints for ep in repo_endpoints):
            if not self.repository_owner or not self.repository_name:
                raise UserException(
                    f"Repository owner and name are required when using endpoints: {', '.join(repo_endpoints)}"
                )
        
        # Validate issue_state
        if self.issue_state not in ["all", "open", "closed"]:
            raise UserException(f"Invalid issue_state: {self.issue_state}. Must be 'all', 'open', or 'closed'")
        
        return self
    
    @property
    def github_token(self) -> str:
        """Get GitHub token from authorization"""
        return self.authorization.github_token
