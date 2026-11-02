"""Source-control operations exposed through the existing authenticated API."""

from uuid import UUID

from fastapi import APIRouter, Depends, Query

from app.source_control import (
    ChangeRequest,
    IntegrationRequest,
    RefRequest,
    SourceControl,
    delta_response,
)
from app.sync import PublishRequest


def source_router(app, authenticate):
    router = APIRouter(prefix="/projects/{project_id}")

    def service(project, principal, write=False):
        (principal.write if write else principal.project)(project)
        return SourceControl(app.state.sync)

    @router.get("/checkout-head")
    def checkout_head(project_id: UUID, principal=Depends(authenticate)):
        sc = service(project_id, principal)
        revision = sc.sync.head(project_id)[0]
        return {
            **revision.model_dump(mode="json", exclude={"files", "directories"}),
            **sc.tree(project_id, revision.revision_id),
        }

    @router.get("/delta")
    def delta(project_id: UUID, base: UUID, principal=Depends(authenticate)):
        sc = service(project_id, principal)
        old = sc.revision(project_id, base)
        new = sc.sync.head(project_id)[0]
        return delta_response(old, new)

    @router.post("/push-delta")
    def push(project_id: UUID, request: ChangeRequest, principal=Depends(authenticate)):
        sc = service(project_id, principal, True)
        old = sc.revision(project_id, request.base_revision)
        candidate = sc.commit(project_id, request, principal.name)
        new = sc.sync.publish(
            project_id,
            PublishRequest(
                base_revision=request.base_revision,
                files=candidate.files,
                directories=candidate.directories,
            ),
            candidate=candidate,
        )
        return delta_response(old, new)

    @router.post("/changesets", status_code=201)
    def commit(project_id: UUID, request: ChangeRequest, principal=Depends(authenticate)):
        sc = service(project_id, principal, True)
        revision = sc.commit(project_id, request, principal.name)
        return revision.model_dump(exclude={"files", "directories"})

    @router.get("/revisions/{revision_id}")
    def revision(project_id: UUID, revision_id: UUID, principal=Depends(authenticate)):
        return service(project_id, principal).revision(project_id, revision_id)

    @router.get("/revisions/{revision_id}/tree")
    def tree(
        project_id: UUID, revision_id: UUID, prefix: str = "", principal=Depends(authenticate)
    ):
        return service(project_id, principal).tree(project_id, revision_id, prefix)

    @router.get("/history/{revision_id}")
    def history(
        project_id: UUID,
        revision_id: UUID,
        path: str | None = None,
        limit: int = Query(100, ge=1, le=1000),
        principal=Depends(authenticate),
    ):
        return service(project_id, principal).history(project_id, revision_id, path, limit)

    @router.get("/diff")
    def diff(
        project_id: UUID,
        left: UUID,
        right: UUID,
        path: str | None = None,
        principal=Depends(authenticate),
    ):
        return service(project_id, principal).diff(project_id, left, right, path)

    @router.get("/merge-base")
    def merge_base(project_id: UUID, left: UUID, right: UUID, principal=Depends(authenticate)):
        return {"revision_id": service(project_id, principal).merge_base(project_id, left, right)}

    @router.get("/blame/{revision_id}")
    def blame(project_id: UUID, revision_id: UUID, path: str, principal=Depends(authenticate)):
        return service(project_id, principal).blame(project_id, revision_id, path)

    @router.post("/merge")
    def merge(project_id: UUID, request: IntegrationRequest, principal=Depends(authenticate)):
        result = service(project_id, principal, True).integrate(
            project_id, request, author=principal.name
        )
        return result.model_dump(exclude={"files", "directories"})

    @router.post("/rebase")
    def rebase(project_id: UUID, request: IntegrationRequest, principal=Depends(authenticate)):
        result = service(project_id, principal, True).integrate(
            project_id, request, True, principal.name
        )
        return result.model_dump(exclude={"files", "directories"})

    @router.put("/refs/{name:path}")
    def update_ref(
        project_id: UUID, name: str, request: RefRequest, principal=Depends(authenticate)
    ):
        return service(project_id, principal, True).update_ref(project_id, name, request)

    @router.get("/refs")
    def refs(project_id: UUID, principal=Depends(authenticate)):
        sc = service(project_id, principal)
        with sc.repo.connect() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT name,revision_id FROM source_refs WHERE project_id=?",
                    (str(project_id),),
                )
            ]

    @router.post("/land/{revision_id}")
    def land(project_id: UUID, revision_id: UUID, expected: UUID, principal=Depends(authenticate)):
        return service(project_id, principal, True).land(project_id, revision_id, expected)

    return router
