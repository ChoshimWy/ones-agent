import { Button } from "@/components/ui/button";
import { ShieldX } from "lucide-react";
import { useNavigate } from "react-router-dom";

export default function ForbiddenPage() {
  const navigate = useNavigate();
  return (
    <div className="flex min-h-screen flex-col items-center justify-center gap-4 text-center">
      <ShieldX className="h-16 w-16 text-muted-foreground" />
      <h1 className="text-2xl font-bold">403 Forbidden</h1>
      <p className="text-muted-foreground">
        You don't have permission to access this page.
      </p>
      <Button variant="outline" onClick={() => navigate(-1)}>
        Go back
      </Button>
    </div>
  );
}
